from __future__ import annotations

import json
import re
import statistics
import urllib.parse
from typing import Any, Optional

from pydantic import BaseModel, Field

from omni_agent.connectors.base import (
    BaseConnector,
    ChannelHarvestResult,
    build_browser_headers,
    is_antibot_challenge,
    polite_get,
)
from omni_agent.connectors.telegram_promos import TelegramPromoScraper
from omni_agent.core.compliance import ComplianceManager
from omni_agent.core.condition_grader import ConditionGrader
from omni_agent.core.landed_cost import LandedCostEngine
from omni_agent.core.models import (
    ChannelTrustTier,
    Coupon,
    NormalizedOffer,
    ShoppingTask,
    VerificationStatus,
)
from omni_agent.core.normalizer import extract_model_codes, normalize_title

_PRICE_RE = re.compile(r"([\d]+(?:[.,]\d+)?)")
_ORDERS_RE = re.compile(r"([\d,]+)\+?\s*(?:sold|orders?)?", re.IGNORECASE)
_COUPON_AMOUNT_RE = re.compile(
    r"(?:\$|USD\s*)?(\d+(?:[.,]\d+)?)\s*(?:\$|USD)?\s*(?:off|від|over|on)\s*(?:\$|USD\s*)?(\d+(?:[.,]\d+)?)?",
    re.IGNORECASE,
)

SUPPORTED_ALIEXPRESS_ROUTES: dict[str, str] = {
    "UA": "USD",
    "PL": "USD",
    "DE": "EUR",
    "CZ": "EUR",
    "FR": "EUR",
    "IT": "EUR",
    "ES": "EUR",
    "US": "USD",
}


class AliExpressAntiBotError(Exception):
    """Raised when AliExpress returns punish/x5sec/captcha challenge."""


class AliExpressParseError(Exception):
    """Raised when embedded JSON cannot be located in AliExpress HTML."""


class AliExpressRawDeal(BaseModel):
    product_id: str
    title: str
    url: str
    image_url: str = ""
    sale_price: float
    original_price: Optional[float] = None
    shipping_cost: float = 0.0
    currency: str = "USD"
    rating: Optional[float] = None
    orders_count: int = 0
    store_name: str = ""
    free_shipping: bool = False
    coupons: list[Coupon] = Field(default_factory=list)
    promo_badges: list[str] = Field(default_factory=list)
    raw_price_snippet: str = ""


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    match = _PRICE_RE.search(text)
    return float(match.group(1)) if match else None


def _parse_orders(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    match = _ORDERS_RE.search(str(value))
    if not match:
        return 0
    return int(match.group(1).replace(",", ""))


def _parse_inline_coupon(tag_text: str, currency: str) -> Coupon:
    m = _COUPON_AMOUNT_RE.search(tag_text)
    if m:
        disc = float(m.group(1).replace(",", "."))
        min_sp = float(m.group(2).replace(",", ".")) if m.group(2) else 0.0
        return Coupon(
            description=tag_text,
            discount_amount=disc,
            min_spend=min_sp,
            currency=currency,
        )
    return Coupon(description=tag_text, currency=currency)


def extract_init_data_json(html: str) -> dict[str, Any]:
    """Extract embedded search JSON (_init_data_ or DCData) from AliExpress search HTML."""
    lower = html.lower()
    if "punish" in lower or "x5sec" in lower or "_____tmd_____" in lower or is_antibot_challenge(200, html):
        raise AliExpressAntiBotError(
            "AliExpress returned an anti-bot/CAPTCHA challenge page (punish/x5sec)."
        )

    markers = [
        "_init_data_=",
        "_init_data_ =",
        "window._d_c_.DCData=",
        "window.runParams=",
    ]
    decoder = json.JSONDecoder()

    for marker in markers:
        pos = 0
        while True:
            idx = html.find(marker, pos)
            if idx == -1:
                break
            pos = idx + len(marker)
            start = html.find("{", pos)
            if start == -1:
                continue
            try:
                obj, _ = decoder.raw_decode(html, start)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                data_match = re.match(r"\{\s*data\s*:\s*\{", html[start : start + 40])
                if data_match:
                    inner_start = html.find("{", start + 1)
                    try:
                        obj, _ = decoder.raw_decode(html, inner_start)
                        if isinstance(obj, dict):
                            return {"data": obj}
                    except json.JSONDecodeError:
                        pass
                continue

    raise AliExpressParseError("Could not locate embedded product JSON in AliExpress search response.")


def _find_item_list(data: Any) -> list[dict[str, Any]]:
    """Recursively locate the itemList array inside AliExpress's nested JSON payload."""
    if isinstance(data, dict):
        if "itemList" in data and isinstance(data["itemList"], dict):
            content = data["itemList"].get("content")
            if isinstance(content, list):
                return [item for item in content if isinstance(item, dict)]
        if "mods" in data and isinstance(data["mods"], dict):
            item_list = data["mods"].get("itemList")
            if isinstance(item_list, dict) and isinstance(item_list.get("content"), list):
                return [item for item in item_list["content"] if isinstance(item, dict)]
        for value in data.values():
            found = _find_item_list(value)
            if found:
                return found
    elif isinstance(data, list):
        for entry in data:
            found = _find_item_list(entry)
            if found:
                return found
    return []


def parse_search_items(payload: dict[str, Any], currency: str = "USD") -> list[AliExpressRawDeal]:
    """Convert raw AliExpress search JSON payload into AliExpressRawDeal models."""
    raw_items = _find_item_list(payload)
    deals: list[AliExpressRawDeal] = []

    for item in raw_items:
        product_id = str(item.get("productId") or item.get("itemId") or "").strip()
        if not product_id:
            continue

        title_obj = item.get("title") or {}
        if isinstance(title_obj, dict):
            title = str(title_obj.get("displayTitle") or title_obj.get("seoTitle") or "").strip()
        else:
            title = str(title_obj).strip()
        if not title:
            continue

        prices = item.get("prices") or {}
        sale_obj = prices.get("salePrice") or {}
        orig_obj = prices.get("originalPrice") or {}

        sale_price = _parse_float(
            sale_obj.get("minPrice") or sale_obj.get("formattedPrice") or item.get("salePrice")
        )
        if sale_price is None or sale_price <= 0:
            continue

        item_currency = str(sale_obj.get("currencyCode") or currency).upper()
        raw_snippet = str(
            sale_obj.get("formattedPrice")
            or f"{sale_price} {item_currency}"
        )

        original_price = _parse_float(
            orig_obj.get("minPrice") or orig_obj.get("formattedPrice") or item.get("originalPrice")
        )

        shipping_cost = 0.0
        free_shipping = False
        promo_badges: list[str] = []
        coupons: list[Coupon] = []

        selling_points = item.get("sellingPoints") or []
        for sp in selling_points:
            if not isinstance(sp, dict):
                continue
            tag_text = str(sp.get("tagContent", {}).get("tagText") or sp.get("text") or "").strip()
            if not tag_text:
                continue
            lower_tag = tag_text.lower()
            if "free shipping" in lower_tag or "free delivery" in lower_tag:
                free_shipping = True
                shipping_cost = 0.0
            elif "shipping" in lower_tag or "delivery" in lower_tag:
                parsed_ship = _parse_float(tag_text)
                if parsed_ship is not None:
                    shipping_cost = parsed_ship
            elif "off" in lower_tag or "coupon" in lower_tag or "save" in lower_tag or "promo" in lower_tag:
                coupons.append(_parse_inline_coupon(tag_text, item_currency))
            else:
                promo_badges.append(tag_text)

        logistics = item.get("logisticsDesc") or item.get("shippingFee")
        if logistics and not free_shipping:
            if "free" in str(logistics).lower():
                free_shipping = True
                shipping_cost = 0.0
            else:
                parsed_ship = _parse_float(logistics)
                if parsed_ship is not None:
                    shipping_cost = parsed_ship

        evaluation = item.get("evaluation") or {}
        rating = _parse_float(evaluation.get("starRating") or item.get("starRating"))
        if rating is not None and rating > 5.0:
            rating = 5.0

        trade = item.get("trade") or {}
        orders_count = _parse_orders(trade.get("tradeDesc") or item.get("orders"))

        store = item.get("store") or {}
        store_name = str(store.get("storeName") or "AliExpress Store")

        image_obj = item.get("image") or {}
        img_url = str(image_obj.get("imgUrl") or "")
        if img_url.startswith("//"):
            img_url = f"https:{img_url}"

        deals.append(
            AliExpressRawDeal(
                product_id=product_id,
                title=title,
                url=f"https://www.aliexpress.com/item/{product_id}.html",
                image_url=img_url,
                sale_price=sale_price,
                original_price=original_price,
                shipping_cost=shipping_cost,
                currency=item_currency,
                rating=rating,
                orders_count=orders_count,
                store_name=store_name,
                free_shipping=free_shipping,
                coupons=coupons,
                promo_badges=promo_badges,
                raw_price_snippet=raw_snippet,
            )
        )

    return deals


class AliExpressConnector(BaseConnector):
    channel_name = "aliexpress"
    default_route_country = "UA"
    default_currency = "USD"

    def __init__(
        self,
        *,
        route_country: str = "UA",
        currency: Optional[str] = None,
        telegram_channels: Optional[list[str]] = None,
        telegram_scraper: Optional[TelegramPromoScraper] = None,
        landed_cost_engine: Optional[LandedCostEngine] = None,
        timeout: float = 20.0,
        client=None,
    ) -> None:
        super().__init__(landed_cost_engine=landed_cost_engine, timeout=timeout, client=client)
        self.route_country = route_country.upper()
        self.currency = (currency or SUPPORTED_ALIEXPRESS_ROUTES.get(self.route_country, "USD")).upper()
        self.telegram_channels = telegram_channels or []
        self.telegram_scraper = telegram_scraper or (
            TelegramPromoScraper(channels=self.telegram_channels, timeout=timeout, client=client)
            if self.telegram_channels
            else None
        )

    def build_search_url(self, query: str) -> str:
        encoded = urllib.parse.quote_plus(query)
        return f"https://www.aliexpress.com/wholesale?SearchText={encoded}"

    def build_cookies(self, route_country: Optional[str] = None, currency: Optional[str] = None) -> dict[str, str]:
        route = (route_country or self.route_country).upper()
        cur = (currency or self.currency).upper()
        return {
            "aep_usuc_f": f"site=glo&c_tp={cur}&region={route}&b_locale=en_US",
        }

    def normalize_deals(
        self,
        raw_deals: list[AliExpressRawDeal],
        task: ShoppingTask,
        *,
        route_country: Optional[str] = None,
        extra_coupons: Optional[list[Coupon]] = None,
    ) -> tuple[list[NormalizedOffer], list[str]]:
        route = (route_country or self.route_country).upper()
        if not raw_deals:
            return [], []

        prices = [d.sale_price for d in raw_deals if d.sale_price > 0]
        median_price = statistics.median(prices) if prices else 0.0

        offers: list[NormalizedOffer] = []
        discovered_mpns: list[str] = []
        seen_mpns: set[str] = set()
        shared_coupons = list(extra_coupons or [])

        for deal in raw_deals:
            is_variant_trap = bool(median_price > 0 and deal.sale_price < 0.28 * median_price)
            all_coupons = list(deal.coupons) + shared_coupons

            # Direct CN->UA AliExpress vs EU forwarder route (PL/DE/CZ/FR/IT/ES/US)
            is_direct_ua = route == "UA"
            calc_route = "CN" if is_direct_ua else route

            landed = self.landed_cost_engine.calculate(
                item_price_raw=deal.sale_price,
                currency=deal.currency,
                route_country=calc_route,
                shipping_raw=deal.shipping_cost,
                weight_kg=task.estimated_weight_kg,
                volumetric_factor=task.volumetric_factor,
                customs_mode=task.customs_mode,
                direct_to_ua_export=is_direct_ua,
                coupons=all_coupons,
                is_aliexpress_cn=True,
            )
            # Keep route_country aligned with selected ship-to route
            landed.route_country = route

            grounded, _ = ComplianceManager.verify_price_grounding(deal.sale_price, deal.raw_price_snippet)
            condition = ConditionGrader.grade(deal.title, raw_condition="New")

            mpns = extract_model_codes(deal.title)
            for code in mpns:
                if code not in seen_mpns:
                    seen_mpns.add(code)
                    discovered_mpns.append(code)

            is_official_store = any(
                k in deal.store_name.lower() for k in ("official", "flagship", "choice", "brand")
            )
            trust_tier = (
                ChannelTrustTier.TIER_1_OFFICIAL if is_official_store else ChannelTrustTier.TIER_2_ESCROW
            )

            specs: dict[str, Any] = {
                "variant_trap": is_variant_trap,
                "batch_median_raw": round(median_price, 2),
                "free_shipping": deal.free_shipping,
                "promo_badges": deal.promo_badges,
            }
            if deal.original_price is not None:
                specs["original_price_raw"] = deal.original_price

            offers.append(
                NormalizedOffer(
                    offer_id=f"ali_{route.lower()}_{deal.product_id}",
                    task_id=task.task_id,
                    channel=self.channel_name,
                    route_country=route,
                    store_name=deal.store_name,
                    trust_tier=trust_tier,
                    title=deal.title,
                    normalized_title=normalize_title(deal.title),
                    mpn_codes=mpns,
                    url=deal.url,
                    image_url=deal.image_url,
                    price_raw=deal.sale_price,
                    currency=deal.currency,
                    shipping_raw=deal.shipping_cost,
                    in_stock=True,
                    delivery_days=14 if route == "UA" else 18,
                    warranty_months=12,
                    seller_rating=deal.rating,
                    orders_or_reviews=deal.orders_count,
                    coupons=all_coupons,
                    condition=condition,
                    landed_cost=landed,
                    verification_status=VerificationStatus.VERIFIED if grounded else VerificationStatus.MANUAL_CHECK_LINK,
                    raw_price_snippet=deal.raw_price_snippet,
                    is_grounded=grounded,
                    specs=specs,
                )
            )

        return offers, discovered_mpns

    def parse_html(
        self,
        html: str,
        task: ShoppingTask,
        *,
        route_country: Optional[str] = None,
        currency: Optional[str] = None,
        extra_coupons: Optional[list[Coupon]] = None,
        search_url: Optional[str] = None,
        best_ua_uah: Optional[float] = None,
    ) -> ChannelHarvestResult:
        route = (route_country or self.route_country).upper()
        cur = (currency or SUPPORTED_ALIEXPRESS_ROUTES.get(route, self.currency)).upper()
        direct_url = search_url or self.build_search_url(self.select_query(task, lang="en"))

        try:
            payload = extract_init_data_json(html)
        except AliExpressAntiBotError as exc:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="ANTIBOT_CHALLENGE_PUNISH",
                route_country=route,
                currency=cur,
                best_ua_uah=best_ua_uah,
                direct_to_ua_export=(route == "UA"),
                error_note=str(exc),
            )
        except AliExpressParseError as exc:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="PARSE_JSON_MISSING",
                route_country=route,
                currency=cur,
                best_ua_uah=best_ua_uah,
                direct_to_ua_export=(route == "UA"),
                error_note=str(exc),
            )

        raw_deals = parse_search_items(payload, currency=cur)
        if not raw_deals:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="EMPTY_ITEM_LIST",
                route_country=route,
                currency=cur,
                best_ua_uah=best_ua_uah,
                direct_to_ua_export=(route == "UA"),
                error_note="AliExpress _init_data_ contained zero items",
            )

        offers, discovered_mpns = self.normalize_deals(
            raw_deals,
            task,
            route_country=route,
            extra_coupons=extra_coupons,
        )
        return ChannelHarvestResult(
            channel=self.channel_name,
            route_country=route,
            status=VerificationStatus.VERIFIED,
            offers=offers,
            discovered_mpns=discovered_mpns,
        )

    async def search(
        self,
        task: ShoppingTask,
        *,
        route_country: Optional[str] = None,
        currency: Optional[str] = None,
        best_ua_uah: Optional[float] = None,
        extra_coupons: Optional[list[Coupon]] = None,
    ) -> ChannelHarvestResult:
        route = (route_country or self.route_country).upper()
        cur = (currency or SUPPORTED_ALIEXPRESS_ROUTES.get(route, self.currency)).upper()
        query = self.select_query(task, lang="en")
        direct_url = self.build_search_url(query)

        collected_coupons = list(extra_coupons or [])
        if self.telegram_scraper is not None and self.telegram_channels:
            collected_coupons.extend(await self.telegram_scraper.collect_coupons(self.telegram_channels))

        headers = build_browser_headers(accept_language="en-US,en;q=0.9")
        cookies = self.build_cookies(route_country=route, currency=cur)

        try:
            status_code, html, final_url = await polite_get(
                "https://www.aliexpress.com/wholesale",
                params={"SearchText": query},
                headers=headers,
                cookies=cookies,
                timeout=self.timeout,
                client=self.client,
            )
        except Exception as exc:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="HTTP_ERROR",
                route_country=route,
                currency=cur,
                best_ua_uah=best_ua_uah,
                direct_to_ua_export=(route == "UA"),
                error_note=str(exc),
            )

        if is_antibot_challenge(status_code, html):
            return self.make_manual_result(
                task=task,
                direct_search_url=final_url or direct_url,
                reason_status=f"ANTIBOT_HTTP_{status_code}",
                route_country=route,
                currency=cur,
                best_ua_uah=best_ua_uah,
                direct_to_ua_export=(route == "UA"),
            )

        return self.parse_html(
            html,
            task,
            route_country=route,
            currency=cur,
            extra_coupons=collected_coupons,
            search_url=direct_url,
            best_ua_uah=best_ua_uah,
        )
