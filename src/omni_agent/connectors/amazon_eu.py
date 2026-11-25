from __future__ import annotations

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
from omni_agent.core.landed_cost import LandedCostEngine
from omni_agent.core.models import (
    ChannelTrustTier,
    ConditionCode,
    NormalizedOffer,
    ShoppingTask,
    VerificationStatus,
)
from omni_agent.core.normalizer import detect_kit_trap, extract_model_codes, normalize_title

AMAZON_EU_DOMAINS: dict[str, tuple[str, str, str]] = {
    "DE": ("www.amazon.de", "EUR", "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"),
    "PL": ("www.amazon.pl", "PLN", "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"),
    "FR": ("www.amazon.fr", "EUR", "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"),
    "IT": ("www.amazon.it", "EUR", "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7"),
    "ES": ("www.amazon.es", "EUR", "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7"),
}


class AmazonEUConnector(BaseConnector):
    channel_name = "amazon_eu"
    default_route_country = "DE"
    default_currency = "EUR"

    def __init__(
        self,
        *,
        country: str = "DE",
        direct_export_shipping_eur: float = 25.0,
        landed_cost_engine: Optional[LandedCostEngine] = None,
        timeout: float = 15.0,
        client=None,
    ) -> None:
        super().__init__(landed_cost_engine=landed_cost_engine, timeout=timeout, client=client)
        self.country = country.upper()
        if self.country not in AMAZON_EU_DOMAINS:
            raise ValueError(f"Unsupported Amazon EU country: {country}")
        self.domain, self.currency, self.accept_language = AMAZON_EU_DOMAINS[self.country]
        self.channel_name = f"amazon_{self.country.lower()}"
        self.default_route_country = self.country
        self.default_currency = self.currency
        self.direct_export_shipping_eur = direct_export_shipping_eur

    def build_search_url(self, query: str, country: Optional[str] = None) -> str:
        c = (country or self.country).upper()
        domain = AMAZON_EU_DOMAINS[c][0]
        encoded = urllib.parse.quote_plus(query)
        return f"https://{domain}/s?k={encoded}"

    @staticmethod
    def _extract_price(card) -> tuple[Optional[float], str]:
        offscreen = card.select_one("span.a-price span.a-offscreen") or card.select_one(".a-price .a-offscreen")
        if offscreen:
            snippet = offscreen.get_text(" ", strip=True)
            val = parse_price_value(snippet)
            if val is not None and val > 0:
                return val, snippet

        whole = card.select_one(".a-price-whole")
        frac = card.select_one(".a-price-fraction")
        symbol = card.select_one(".a-price-symbol")
        if whole:
            w_txt = re.sub(r"[^\d]", "", whole.get_text(strip=True))
            f_txt = re.sub(r"[^\d]", "", frac.get_text(strip=True)) if frac else "00"
            sym_txt = symbol.get_text(strip=True) if symbol else ""
            if w_txt:
                snippet = f"{w_txt}.{f_txt or '00'} {sym_txt}".strip()
                try:
                    return float(f"{w_txt}.{f_txt or '00'}"), snippet
                except ValueError:
                    pass
        return None, ""

    @staticmethod
    def _extract_secondary_warehouse_price(card, primary_price: float) -> Optional[tuple[float, str]]:
        """
        Detect Amazon Warehouse / Retourenkauf / Used - Like New / Weitere Kaufangebote secondary price.
        """
        secondary_block = card.select_one("div[data-cy='secondary-offer-recipe'], .s-secondary-offer")
        text_pool = secondary_block.get_text(" ", strip=True) if secondary_block else ""
        if not text_pool:
            card_txt = card.get_text(" ", strip=True)
            if re.search(
                r"(?:gebraucht|retourenkauf|warehouse|used|like new|wie neu|używany|d'occasion|usato|de segunda mano)",
                card_txt,
                re.IGNORECASE,
            ):
                text_pool = card_txt

        if not text_pool:
            return None

        # Match patterns like "Gebraucht: 499,00 €" or "ab 499,00 € (2 gebrauchte und neue Artikel)"
        m = re.search(
            r"(?:gebraucht|retourenkauf|warehouse|used|wie neu|like new|używany|d'occasion|usato|de segunda mano|ab|od|from)[^0-9]{0,25}(\d+(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?\s*(?:€|EUR|zł|PLN))",
            text_pool,
            re.IGNORECASE,
        )
        if not m:
            return None

        snippet = m.group(1).strip()
        val = parse_price_value(snippet)
        if val is not None and 0 < val < primary_price:
            return val, snippet
        return None

    def _compute_landed_with_dual_route(
        self,
        *,
        price_raw: float,
        currency: str,
        country: str,
        task: ShoppingTask,
        is_warehouse: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        if country == "DE":
            # Route A: Direct export to UA (-19% DE VAT + direct shipping estimate)
            direct_ship_eur = self.direct_export_shipping_eur if not is_warehouse else self.direct_export_shipping_eur
            direct_lc = self.landed_cost_engine.calculate(
                item_price_raw=price_raw,
                currency=currency,
                route_country="DE",
                shipping_raw=direct_ship_eur,
                weight_kg=task.estimated_weight_kg,
                volumetric_factor=task.volumetric_factor,
                customs_mode=task.customs_mode,
                direct_to_ua_export=True,
            )
            # Route B: Forwarder warehouse (NP Shopping DE / Meest DE, keeps 19% VAT)
            fw_lc = self.landed_cost_engine.calculate(
                item_price_raw=price_raw,
                currency=currency,
                route_country="DE",
                shipping_raw=0.0,
                weight_kg=task.estimated_weight_kg,
                volumetric_factor=task.volumetric_factor,
                customs_mode=task.customs_mode,
                direct_to_ua_export=False,
            )
            chosen = direct_lc if direct_lc.realistic_landed_uah <= fw_lc.realistic_landed_uah else fw_lc
            dual_meta = {
                "direct_export_uah": direct_lc.realistic_landed_uah,
                "forwarder_uah": fw_lc.realistic_landed_uah,
                "chosen_mode": "direct_export" if chosen is direct_lc else "forwarder_de",
                "savings_uah": round(abs(fw_lc.realistic_landed_uah - direct_lc.realistic_landed_uah), 2),
            }
            return chosen, dual_meta

        lc = self.landed_cost_engine.calculate(
            item_price_raw=price_raw,
            currency=currency,
            route_country=country,
            shipping_raw=0.0,
            weight_kg=task.estimated_weight_kg,
            volumetric_factor=task.volumetric_factor,
            customs_mode=task.customs_mode,
            direct_to_ua_export=False,
        )
        return lc, {}

    def parse_html(
        self,
        html: str,
        task: ShoppingTask,
        *,
        country: Optional[str] = None,
        search_url: Optional[str] = None,
        best_ua_uah: Optional[float] = None,
    ) -> ChannelHarvestResult:
        c = (country or self.country).upper()
        domain, currency, _ = AMAZON_EU_DOMAINS[c]
        lang_key = c.lower() if c.lower() in task.queries_i18n else "en"
        query = self.select_query(task, lang=lang_key, prefer_mpn=bool(task.canonical_mpns))
        direct_url = search_url or self.build_search_url(query, country=c)

        if not html or is_antibot_challenge(200, html):
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="ANTIBOT_AMAZON_CAPTCHA",
                route_country=c,
                currency=currency,
                best_ua_uah=best_ua_uah,
                direct_to_ua_export=(c == "DE"),
                shipping_raw=self.direct_export_shipping_eur if c == "DE" else 0.0,
                error_note=f"Amazon {c} returned CAPTCHA / anti-bot challenge",
            )

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select('div[data-component-type="s-search-result"]')

        offers: list[NormalizedOffer] = []
        discovered_mpns: list[str] = []
        seen_mpns: set[str] = set()

        for card in cards:
            asin = (card.get("data-asin") or "").strip()
            if not asin:
                continue

            h2_el = card.select_one("h2")
            if not h2_el:
                continue
            title = h2_el.get_text(" ", strip=True)
            if not title or len(title) < 4:
                continue

            link_el = h2_el.find("a", href=True) or card.select_one("a.a-link-normal[href]")
            href = (link_el.get("href") or f"/dp/{asin}").strip() if link_el else f"/dp/{asin}"
            full_url = f"https://{domain}{href}" if href.startswith("/") else href

            img_el = card.select_one("img.s-image") or card.find("img")
            img_url = (img_el.get("src") or "").strip() if img_el else ""

            price_val, raw_snippet = self._extract_price(card)
            if price_val is None or price_val <= 0:
                continue

            grounded, _ = ComplianceManager.verify_price_grounding(price_val, raw_snippet)

            rating: Optional[float] = None
            rating_el = card.select_one("span.a-icon-alt")
            if rating_el:
                m_rat = re.search(r"(\d+(?:[.,]\d+)?)", rating_el.get_text(" ", strip=True))
                if m_rat:
                    rating = round(float(m_rat.group(1).replace(",", ".")), 2)

            reviews_count = 0
            rev_el = card.select_one("span.a-size-base.s-underline-text, a[href*='#customerReviews'] span")
            if rev_el:
                digits = re.sub(r"[^\d]", "", rev_el.get_text(strip=True))
                if digits:
                    reviews_count = int(digits)

            mpns = extract_model_codes(title)
            for code in mpns:
                if code not in seen_mpns:
                    seen_mpns.add(code)
                    discovered_mpns.append(code)

            kit_trap = detect_kit_trap(title)
            condition = ConditionGrader.grade(title, raw_condition="New")

            landed, dual_meta = self._compute_landed_with_dual_route(
                price_raw=price_val,
                currency=currency,
                country=c,
                task=task,
            )

            specs: dict[str, Any] = {"asin": asin}
            if dual_meta:
                specs["dual_route_comparison"] = dual_meta
            if kit_trap:
                specs["kit_trap"] = kit_trap

            offers.append(
                NormalizedOffer(
                    offer_id=f"amz_{c.lower()}_{asin}",
                    task_id=task.task_id,
                    channel=f"amazon_{c.lower()}",
                    route_country=c,
                    store_name=f"Amazon {c}",
                    trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
                    title=title,
                    normalized_title=normalize_title(title),
                    mpn_codes=mpns,
                    url=full_url,
                    image_url=img_url,
                    price_raw=price_val,
                    currency=currency,
                    shipping_raw=self.direct_export_shipping_eur if landed.direct_to_ua_export else 0.0,
                    in_stock=True,
                    delivery_days=8 if c == "DE" else 10,
                    warranty_months=24,
                    seller_rating=rating,
                    orders_or_reviews=reviews_count,
                    condition=condition,
                    landed_cost=landed,
                    verification_status=VerificationStatus.VERIFIED if grounded else VerificationStatus.MANUAL_CHECK_LINK,
                    raw_price_snippet=raw_snippet,
                    is_grounded=grounded,
                    specs=specs,
                )
            )

            # Secondary Amazon Warehouse / Retourenkauf / Used - Like New offer
            wh = self._extract_secondary_warehouse_price(card, price_val)
            if wh is not None:
                wh_price, wh_snippet = wh
                wh_grounded, _ = ComplianceManager.verify_price_grounding(wh_price, wh_snippet)
                wh_cond = ConditionGrader.grade(
                    f"{title} Amazon Warehouse Retourenkauf Like New",
                    raw_condition="Open Box",
                )
                # Ensure Open Box code
                if wh_cond.code != ConditionCode.OPEN_BOX:
                    wh_cond = ConditionGrader.grade("Amazon Warehouse Retourenkauf")

                wh_landed, wh_dual = self._compute_landed_with_dual_route(
                    price_raw=wh_price,
                    currency=currency,
                    country=c,
                    task=task,
                    is_warehouse=True,
                )
                wh_specs: dict[str, Any] = {"asin": asin, "offer_type": "amazon_warehouse"}
                if wh_dual:
                    wh_specs["dual_route_comparison"] = wh_dual
                if kit_trap:
                    wh_specs["kit_trap"] = kit_trap

                offers.append(
                    NormalizedOffer(
                        offer_id=f"amz_{c.lower()}_{asin}_warehouse",
                        task_id=task.task_id,
                        channel=f"amazon_{c.lower()}_warehouse",
                        route_country=c,
                        store_name=f"Amazon Warehouse {c} (Retourenkauf)",
                        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
                        title=f"{title} [Amazon Warehouse / Retourenkauf]",
                        normalized_title=normalize_title(title),
                        mpn_codes=mpns,
                        url=full_url,
                        image_url=img_url,
                        price_raw=wh_price,
                        currency=currency,
                        shipping_raw=self.direct_export_shipping_eur if wh_landed.direct_to_ua_export else 0.0,
                        in_stock=True,
                        delivery_days=8 if c == "DE" else 10,
                        warranty_months=24,
                        seller_rating=rating,
                        orders_or_reviews=reviews_count,
                        condition=wh_cond,
                        landed_cost=wh_landed,
                        verification_status=VerificationStatus.VERIFIED if wh_grounded else VerificationStatus.MANUAL_CHECK_LINK,
                        raw_price_snippet=wh_snippet,
                        is_grounded=wh_grounded,
                        specs=wh_specs,
                    )
                )

        if not offers:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="EMPTY_DOM_OR_CAPTCHA",
                route_country=c,
                currency=currency,
                best_ua_uah=best_ua_uah,
                direct_to_ua_export=(c == "DE"),
                shipping_raw=self.direct_export_shipping_eur if c == "DE" else 0.0,
                error_note=f"Amazon {c} returned no parseable search results",
            )

        return ChannelHarvestResult(
            channel=f"amazon_{c.lower()}",
            route_country=c,
            status=VerificationStatus.VERIFIED,
            offers=offers,
            discovered_mpns=discovered_mpns,
        )

    async def search(
        self,
        task: ShoppingTask,
        *,
        country: Optional[str] = None,
        best_ua_uah: Optional[float] = None,
    ) -> ChannelHarvestResult:
        c = (country or self.country).upper()
        domain, currency, accept_lang = AMAZON_EU_DOMAINS[c]
        lang_key = c.lower() if c.lower() in task.queries_i18n else "en"
        query = self.select_query(task, lang=lang_key, prefer_mpn=bool(task.canonical_mpns))
        url = self.build_search_url(query, country=c)
        headers = build_browser_headers(referer=f"https://{domain}/", accept_language=accept_lang)

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
                route_country=c,
                currency=currency,
                best_ua_uah=best_ua_uah,
                direct_to_ua_export=(c == "DE"),
                shipping_raw=self.direct_export_shipping_eur if c == "DE" else 0.0,
                error_note=str(exc),
            )

        if is_antibot_challenge(status_code, html):
            return self.make_manual_result(
                task=task,
                direct_search_url=final_url or url,
                reason_status=f"ANTIBOT_HTTP_{status_code}",
                route_country=c,
                currency=currency,
                best_ua_uah=best_ua_uah,
                direct_to_ua_export=(c == "DE"),
                shipping_raw=self.direct_export_shipping_eur if c == "DE" else 0.0,
            )

        return self.parse_html(
            html,
            task,
            country=c,
            search_url=url,
            best_ua_uah=best_ua_uah,
        )
