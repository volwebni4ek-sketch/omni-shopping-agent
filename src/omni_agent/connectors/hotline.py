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
    parse_price_range,
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


class HotlineConnector(BaseConnector):
    channel_name = "hotline"
    default_route_country = "UA"
    default_currency = "UAH"

    def build_search_url(self, query: str) -> str:
        encoded = urllib.parse.quote(query)
        return f"https://hotline.ua/ua/sr/?q={encoded}"

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
                error_note="Hotline HTML triggered anti-bot / WAF detection",
            )

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(
            "div.list-item, div.product-item, li.product-item, div[data-product-id], "
            "div.item-info, article.product-card"
        )
        if not cards:
            # Fallback: look for anchors pointing to /ua/ or product paths alongside price text
            candidates = []
            for el in soup.find_all(["div", "li", "article"]):
                txt = el.get_text(" ", strip=True)
                if ("грн" in txt or "₴" in txt) and el.find("a", href=True):
                    if 15 < len(txt) < 600:
                        candidates.append(el)
            cards = candidates

        offers: list[NormalizedOffer] = []
        discovered_mpns: list[str] = []
        seen_mpns: set[str] = set()
        seen_urls: set[str] = set()

        for idx, card in enumerate(cards):
            title_el = (
                card.select_one("a.item-title, .list-item__title a, .product-item__title a, h3 a, h4 a")
                or card.find("a", href=True)
            )
            if not title_el:
                continue

            title = title_el.get_text(" ", strip=True)
            if not title or len(title) < 4:
                continue

            href = title_el.get("href", "").strip()
            if not href:
                a_fallback = card.find("a", href=True)
                href = a_fallback.get("href", "").strip() if a_fallback else ""
            if not href:
                continue

            full_url = f"https://hotline.ua{href}" if href.startswith("/") else href
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            price_el = card.select_one(
                ".list-item__value-price, .price-md, .price__value, .product-item__price, "
                "[data-qaid='product_price'], .price"
            )
            raw_price_snippet = price_el.get_text(" ", strip=True) if price_el else ""
            if not raw_price_snippet:
                # Search for a line/span containing грн or ₴
                for node in card.find_all(["span", "div", "p", "strong", "b"]):
                    nt = node.get_text(" ", strip=True)
                    if ("грн" in nt.lower() or "₴" in nt) and re.search(r"\d", nt) and len(nt) <= 60:
                        raw_price_snippet = nt
                        break

            min_price, max_price = parse_price_range(raw_price_snippet)
            if min_price is None or min_price <= 0:
                continue

            grounded, _ = ComplianceManager.verify_price_grounding(min_price, raw_price_snippet)

            mpns = extract_model_codes(title)
            for code in mpns:
                if code not in seen_mpns:
                    seen_mpns.add(code)
                    discovered_mpns.append(code)

            img_tag = card.find("img")
            img_url = ""
            if img_tag:
                img_url = (img_tag.get("src") or img_tag.get("data-src") or "").strip()
                if img_url.startswith("//"):
                    img_url = f"https:{img_url}"

            store_el = card.select_one(".list-item__shop-title, .shop-title, .seller-name")
            store_name = store_el.get_text(" ", strip=True) if store_el else "Hotline Catalog (Official UA)"
            offers_count_el = card.select_one(".list-item__offers-count, .offers-count")
            offers_count = 1
            if offers_count_el:
                m_cnt = re.search(r"(\d+)", offers_count_el.get_text(" ", strip=True))
                if m_cnt:
                    offers_count = int(m_cnt.group(1))

            # Official / catalog matched -> TIER_1_OFFICIAL
            is_official = True
            trust_tier = ChannelTrustTier.TIER_1_OFFICIAL if is_official else ChannelTrustTier.TIER_2_ESCROW

            condition = ConditionGrader.grade(title, raw_condition="Нове")
            landed = self.landed_cost_engine.calculate(
                item_price_raw=min_price,
                currency="UAH",
                route_country="UA",
                weight_kg=task.estimated_weight_kg,
                volumetric_factor=task.volumetric_factor,
                customs_mode=task.customs_mode,
            )

            offer_hash = hashlib.sha1(full_url.encode("utf-8")).hexdigest()[:10]
            specs: dict[str, object] = {"offers_count": offers_count}
            if max_price is not None and max_price > min_price:
                specs["price_range_max_uah"] = max_price

            offers.append(
                NormalizedOffer(
                    offer_id=f"hotline_{offer_hash}_{idx}",
                    task_id=task.task_id,
                    channel=self.channel_name,
                    route_country="UA",
                    store_name=store_name,
                    trust_tier=trust_tier,
                    title=title,
                    normalized_title=normalize_title(title),
                    mpn_codes=mpns,
                    url=full_url,
                    image_url=img_url,
                    price_raw=min_price,
                    currency="UAH",
                    shipping_raw=0.0,
                    in_stock=True,
                    delivery_days=1,
                    warranty_months=24,
                    orders_or_reviews=offers_count,
                    condition=condition,
                    landed_cost=landed,
                    verification_status=VerificationStatus.VERIFIED if grounded else VerificationStatus.MANUAL_CHECK_LINK,
                    raw_price_snippet=raw_price_snippet,
                    is_grounded=grounded,
                    specs=specs,
                )
            )

        if not offers:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="EMPTY_DOM_OR_WAF",
                error_note="Hotline returned no parseable product cards",
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
                referer="https://hotline.ua/",
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
