from __future__ import annotations

import hashlib
import re
import urllib.parse
from typing import Any, Optional

from bs4 import BeautifulSoup

from omni_agent.connectors.base import (
    BaseConnector,
    ChannelHarvestResult,
    build_browser_headers,
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
from omni_agent.core.normalizer import detect_kit_trap, extract_model_codes, normalize_title


class CeneoPLConnector(BaseConnector):
    channel_name = "ceneo_pl"
    default_route_country = "PL"
    default_currency = "PLN"

    def build_search_url(self, query: str) -> str:
        encoded = urllib.parse.quote_plus(query)
        return f"https://www.ceneo.pl/;szukaj-{encoded}"

    def _select_ceneo_query(self, task: ShoppingTask) -> str:
        if task.canonical_mpns:
            return self.select_query(task, lang="pl", prefer_mpn=True)
        return self.select_query(task, lang="pl", prefer_mpn=False)

    def parse_html(
        self,
        html: str,
        task: ShoppingTask,
        *,
        search_url: Optional[str] = None,
        best_ua_uah: Optional[float] = None,
    ) -> ChannelHarvestResult:
        query = self._select_ceneo_query(task)
        direct_url = search_url or self.build_search_url(query)

        if not html or is_antibot_challenge(200, html):
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="ANTIBOT_CLOUDFLARE_OR_DATADOME",
                route_country="PL",
                currency="PLN",
                best_ua_uah=best_ua_uah,
                error_note="Ceneo.pl returned Cloudflare / DataDome challenge",
            )

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(
            "div.cat-prod-row, div.js_category-list-item, div.cat-prod-box, "
            "div[data-pid], article.product-box"
        )
        if not cards:
            candidates = []
            for el in soup.find_all(["div", "article", "li"]):
                txt = el.get_text(" ", strip=True)
                if ("zł" in txt.lower() or "pln" in txt.lower()) and el.find("a", href=True) and 15 < len(txt) < 500:
                    candidates.append(el)
            cards = candidates

        offers: list[NormalizedOffer] = []
        discovered_mpns: list[str] = []
        seen_mpns: set[str] = set()
        seen_urls: set[str] = set()

        for idx, card in enumerate(cards):
            title_el = (
                card.select_one(
                    "strong.cat-prod-row__name a, a.go-to-product, .cat-prod-box__name a, "
                    "h3 a, h4 a"
                )
                or card.find("a", href=True)
            )
            if not title_el:
                continue

            title = title_el.get_text(" ", strip=True)
            if not title or len(title) < 4:
                continue

            link_el = title_el if title_el.name == "a" else card.find("a", href=True)
            href = (link_el.get("href") or "").strip() if link_el else ""
            if not href:
                continue
            full_url = f"https://www.ceneo.pl{href}" if href.startswith("/") else href
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            price_el = card.select_one(
                "span.price, .cat-prod-row__price .price-format, .price-format, .cat-prod-box__price"
            )
            raw_snippet = price_el.get_text(" ", strip=True) if price_el else ""
            if not raw_snippet:
                for node in card.find_all(["span", "div", "strong", "a"]):
                    nt = node.get_text(" ", strip=True)
                    if ("zł" in nt.lower() or "pln" in nt.lower()) and re.search(r"\d", nt) and len(nt) <= 45:
                        raw_snippet = nt
                        break

            price_val = parse_price_value(raw_snippet)
            if price_val is None or price_val <= 0:
                continue

            grounded, _ = ComplianceManager.verify_price_grounding(price_val, raw_snippet)

            img_el = card.find("img")
            img_url = ""
            if img_el:
                img_url = (img_el.get("src") or img_el.get("data-original") or img_el.get("data-src") or "").strip()
                if img_url.startswith("//"):
                    img_url = f"https:{img_url}"

            shop_el = card.select_one(".shop-numb, .cat-prod-row__offers-count, .offers-count")
            offers_cnt = 1
            if shop_el:
                m_cnt = re.search(r"(\d+)", shop_el.get_text(" ", strip=True))
                if m_cnt:
                    offers_cnt = int(m_cnt.group(1))

            rating: Optional[float] = None
            score_el = card.select_one(".product-score, .cat-prod-row__score")
            if score_el:
                m_rat = re.search(r"(\d+(?:[.,]\d+)?)", score_el.get_text(" ", strip=True))
                if m_rat:
                    rating = round(float(m_rat.group(1).replace(",", ".")), 2)

            mpns = extract_model_codes(title)
            for code in mpns:
                if code not in seen_mpns:
                    seen_mpns.add(code)
                    discovered_mpns.append(code)

            kit_trap_reason = detect_kit_trap(title)
            condition = ConditionGrader.grade(title, raw_condition="Nowy")

            # Route PL via Meest Poland / NP Shopping PL + 2% FX spread handled automatically by LandedCostEngine
            landed = self.landed_cost_engine.calculate(
                item_price_raw=price_val,
                currency="PLN",
                route_country="PL",
                shipping_raw=0.0,
                weight_kg=task.estimated_weight_kg,
                volumetric_factor=task.volumetric_factor,
                customs_mode=task.customs_mode,
                direct_to_ua_export=False,
            )

            specs: dict[str, Any] = {"offers_count": offers_cnt}
            if kit_trap_reason:
                specs["kit_trap"] = kit_trap_reason

            pid = (card.get("data-pid") or "").strip()
            offer_id = f"ceneo_pl_{pid}" if pid else f"ceneo_pl_{hashlib.sha1(full_url.encode('utf-8')).hexdigest()[:10]}_{idx}"

            offers.append(
                NormalizedOffer(
                    offer_id=offer_id,
                    task_id=task.task_id,
                    channel=self.channel_name,
                    route_country="PL",
                    store_name=f"Ceneo.pl ({offers_cnt} sklepów)" if offers_cnt > 1 else "Ceneo.pl Catalog",
                    trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
                    title=title,
                    normalized_title=normalize_title(title),
                    mpn_codes=mpns,
                    url=full_url,
                    image_url=img_url,
                    price_raw=price_val,
                    currency="PLN",
                    shipping_raw=0.0,
                    in_stock=True,
                    delivery_days=6,
                    warranty_months=24,
                    seller_rating=rating,
                    orders_or_reviews=offers_cnt,
                    condition=condition,
                    landed_cost=landed,
                    verification_status=VerificationStatus.VERIFIED if grounded else VerificationStatus.MANUAL_CHECK_LINK,
                    raw_price_snippet=raw_snippet,
                    is_grounded=grounded,
                    specs=specs,
                )
            )

        if not offers:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="EMPTY_DOM_OR_ANTIBOT",
                route_country="PL",
                currency="PLN",
                best_ua_uah=best_ua_uah,
                error_note="Ceneo.pl returned no parseable product rows",
            )

        return ChannelHarvestResult(
            channel=self.channel_name,
            route_country="PL",
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
        query = self._select_ceneo_query(task)
        url = self.build_search_url(query)
        headers = build_browser_headers(
            referer="https://www.ceneo.pl/",
            accept_language="pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        )
        try:
            status_code, html, final_url = await polite_get(
                url,
                headers=headers,
                timeout=self.timeout,
                client=self.client,
            )
        except Exception as exc:
            return self.make_manual_result(
                task=task,
                direct_search_url=url,
                reason_status="HTTP_ERROR",
                route_country="PL",
                currency="PLN",
                best_ua_uah=best_ua_uah,
                error_note=str(exc),
            )

        if is_antibot_challenge(status_code, html):
            return self.make_manual_result(
                task=task,
                direct_search_url=final_url or url,
                reason_status=f"ANTIBOT_HTTP_{status_code}",
                route_country="PL",
                currency="PLN",
                best_ua_uah=best_ua_uah,
            )

        return self.parse_html(html, task, search_url=url, best_ua_uah=best_ua_uah)
