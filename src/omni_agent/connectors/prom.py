from __future__ import annotations

import hashlib
import re
import urllib.parse
from typing import Optional

from bs4 import BeautifulSoup

from omni_agent.connectors.base import (
    BaseConnector,
    ChannelHarvestResult,
    is_antibot_challenge,
    parse_price_value,
    polite_get,
)
from omni_agent.core.compliance import ComplianceManager
from omni_agent.core.condition_grader import ConditionGrader
from omni_agent.core.models import (
    ChannelTrustTier,
    NormalizedOffer,
    ShoppingTask,
    VerificationStatus,
)
from omni_agent.core.normalizer import extract_model_codes, normalize_title


class PromConnector(BaseConnector):
    channel_name = "prom"
    default_route_country = "UA"
    default_currency = "UAH"

    def build_search_url(self, query: str) -> str:
        encoded = urllib.parse.quote_plus(query)
        return f"https://prom.ua/ua/search?search_term={encoded}"

    def parse_html(
        self,
        html: str,
        task: ShoppingTask,
        *,
        search_url: Optional[str] = None,
    ) -> ChannelHarvestResult:
        direct_url = search_url or self.build_search_url(self.select_query(task, lang="uk"))
        if not html or is_antibot_challenge(200, html):
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="ANTIBOT_OR_WAF_BLOCKED",
                error_note="Prom.ua HTML triggered anti-bot / WAF detection",
            )

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all(attrs={"data-qaid": "product_block"})

        offers: list[NormalizedOffer] = []
        discovered_mpns: list[str] = []
        seen_mpns: set[str] = set()
        seen_urls: set[str] = set()

        for idx, card in enumerate(cards):
            a_link = card.find(attrs={"data-qaid": "product_link"}) or card.find("a")
            if not a_link:
                continue

            href = (a_link.get("href") or "").strip()
            if not href:
                continue
            full_url = f"https://prom.ua{href}" if href.startswith("/") else href
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            title_elem = card.find(attrs={"data-qaid": "product_name"}) or a_link
            title = title_elem.get_text(" ", strip=True) if title_elem else ""
            if not title or len(title) < 4:
                continue

            price_elem = card.find(attrs={"data-qaid": "product_price"})
            raw_price_snippet = price_elem.get_text(" ", strip=True) if price_elem else ""
            price_val = parse_price_value(raw_price_snippet)
            if price_val is None or price_val <= 20:
                continue

            grounded, _ = ComplianceManager.verify_price_grounding(price_val, raw_price_snippet)

            img_tag = card.find("img")
            img_url = ""
            if img_tag:
                img_url = (img_tag.get("src") or img_tag.get("data-src") or "").strip()

            company_elem = card.find(attrs={"data-qaid": "company_name"})
            company_name = company_elem.get_text(" ", strip=True) if company_elem else "Магазин Prom"

            rating_elem = card.find(attrs={"data-qaid": "company_rating"}) or card.find(
                attrs={"data-qaid": "rating_block"}
            )
            seller_rating: Optional[float] = None
            if rating_elem:
                rating_txt = rating_elem.get_text(" ", strip=True).replace("%", "").replace(",", ".")
                m_rat = re.search(r"(\d+(?:\.\d+)?)", rating_txt)
                if m_rat:
                    val = float(m_rat.group(1))
                    # Prom ratings are often 0-100% or 0-5.0
                    seller_rating = round(val / 20.0, 2) if val > 5.0 else round(val, 2)

            presence_elem = card.find(attrs={"data-qaid": "product_presence"})
            presence_text = presence_elem.get_text(" ", strip=True) if presence_elem else "В наявності"
            in_stock = "немає" not in presence_text.lower() and "відсутн" not in presence_text.lower()

            title_lower = title.lower()
            if any(w in title_lower for w in ["б/у", "б/в", "вживан", "колишній у використанні"]):
                raw_cond = "Вживане"
            elif any(w in title_lower for w in ["refurbished", "відновлен", "перебран", "після сервісу"]):
                raw_cond = "Відновлений"
            elif any(w in title_lower for w in ["open box", "вітринн", "розпакован", "уцінка"]):
                raw_cond = "Open Box"
            else:
                raw_cond = "Нове"

            condition = ConditionGrader.grade(title, description=presence_text, raw_condition=raw_cond)

            mpns = extract_model_codes(title)
            for code in mpns:
                if code not in seen_mpns:
                    seen_mpns.add(code)
                    discovered_mpns.append(code)

            landed = self.landed_cost_engine.calculate(
                item_price_raw=price_val,
                currency="UAH",
                route_country="UA",
                weight_kg=task.estimated_weight_kg,
                volumetric_factor=task.volumetric_factor,
                customs_mode=task.customs_mode,
                local_escrow_shipping_uah=70.0,
            )

            match_id = re.search(r"p(\d+)", href) or re.search(r"m(\d+)", href)
            prod_id = (
                f"prom_{match_id.group(1)}"
                if match_id
                else f"prom_{hashlib.sha1(full_url.encode('utf-8')).hexdigest()[:10]}_{idx}"
            )

            offers.append(
                NormalizedOffer(
                    offer_id=prod_id,
                    task_id=task.task_id,
                    channel=self.channel_name,
                    route_country="UA",
                    store_name=company_name,
                    trust_tier=ChannelTrustTier.TIER_2_ESCROW,
                    title=title,
                    normalized_title=normalize_title(title),
                    mpn_codes=mpns,
                    url=full_url,
                    image_url=img_url,
                    price_raw=price_val,
                    currency="UAH",
                    shipping_raw=0.0,
                    in_stock=in_stock,
                    delivery_days=2,
                    warranty_months=12,
                    seller_rating=seller_rating,
                    condition=condition,
                    landed_cost=landed,
                    verification_status=VerificationStatus.VERIFIED if grounded else VerificationStatus.MANUAL_CHECK_LINK,
                    raw_price_snippet=raw_price_snippet,
                    is_grounded=grounded,
                    specs={"presence": presence_text, "escrow": "Prom-оплата"},
                )
            )

        if not offers:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="EMPTY_DOM_OR_WAF",
                error_note="Prom.ua returned no parseable product_block cards",
            )

        return ChannelHarvestResult(
            channel=self.channel_name,
            route_country="UA",
            status=VerificationStatus.VERIFIED,
            offers=offers,
            discovered_mpns=discovered_mpns,
        )

    async def search(
        self,
        task: ShoppingTask,
        *,
        best_ua_uah: Optional[float] = None,
    ) -> ChannelHarvestResult:
        query = self.select_query(task, lang="uk")
        url = self.build_search_url(query)
        try:
            status_code, html, final_url = await polite_get(
                url,
                timeout=self.timeout,
                client=self.client,
                referer="https://prom.ua/",
            )
        except Exception as exc:
            return self.make_manual_result(
                task=task,
                direct_search_url=url,
                reason_status="HTTP_ERROR",
                best_ua_uah=best_ua_uah,
                error_note=str(exc),
            )

        if is_antibot_challenge(status_code, html):
            return self.make_manual_result(
                task=task,
                direct_search_url=final_url or url,
                reason_status=f"ANTIBOT_HTTP_{status_code}",
                best_ua_uah=best_ua_uah,
            )

        result = self.parse_html(html, task, search_url=url)
        if result.manual_link is not None and best_ua_uah:
            return self.make_manual_result(
                task=task,
                direct_search_url=url,
                reason_status=result.manual_link.reason_status,
                best_ua_uah=best_ua_uah,
                error_note=result.error_note,
            )
        return result
