from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any, Optional

from omni_agent.connectors.base import (
    BaseConnector,
    ChannelHarvestResult,
    build_browser_headers,
    is_antibot_challenge,
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


CATEGORY_SECTION_MAP = {
    "monitors_large_format": "80089",
    "ai_mini_pc": "80095",
}


class RozetkaConnector(BaseConnector):
    channel_name = "rozetka"
    default_route_country = "UA"
    default_currency = "UAH"

    def build_search_url(self, query: str) -> str:
        encoded = urllib.parse.quote(query)
        return f"https://rozetka.com.ua/ua/search/?text={encoded}&seller=rozetka"

    def build_api_url(self, query: str, category: str = "") -> str:
        encoded = urllib.parse.quote(query)
        base = f"https://search.rozetka.com.ua/ua/search/api/v6/?front-type=xl&country=UA&lang=ua&text={encoded}"
        section_id = CATEGORY_SECTION_MAP.get((category or "").strip().lower())
        if section_id:
            return f"{base}&section_id={section_id}"
        return base

    def build_product_main_url(self, goods_id: str | int) -> str:
        return f"https://product-api.rozetka.com.ua/v4/goods/get-main?front-type=xl&country=UA&lang=ua&goodsId={goods_id}"

    def parse_api_payload(
        self,
        payload: dict[str, Any],
        task: ShoppingTask,
        *,
        search_url: Optional[str] = None,
    ) -> ChannelHarvestResult:
        direct_url = search_url or self.build_search_url(self.select_query(task, lang="uk"))
        data = payload.get("data") or payload
        goods = data.get("goods") or data.get("items") or []
        if not isinstance(goods, list) or not goods:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="EMPTY_API_RESULT",
                error_note="Rozetka search API returned no goods",
            )

        offers: list[NormalizedOffer] = []
        discovered_mpns: list[str] = []
        seen_mpns: set[str] = set()

        for item in goods:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            title = str(item.get("title") or item.get("name") or "").strip()
            if not item_id or not title:
                continue

            raw_price_val = item.get("price")
            if raw_price_val is None:
                continue
            try:
                price_val = float(raw_price_val)
            except (TypeError, ValueError):
                continue
            if price_val <= 0:
                continue

            raw_price_snippet = str(item.get("price_pcs") or f"{int(price_val) if price_val.is_integer() else price_val} ₴")
            grounded, _ = ComplianceManager.verify_price_grounding(price_val, raw_price_snippet)

            href = str(item.get("href") or f"https://rozetka.com.ua/ua/p{item_id}/").strip()
            if href.startswith("/"):
                href = f"https://rozetka.com.ua{href}"

            img_url = str(item.get("image_main") or item.get("image") or "").strip()

            seller_id = item.get("seller_id")
            raw_seller = item.get("seller")
            is_official = (
                seller_id == 5
                or str(seller_id) == "5"
                or item.get("is_rozetka_seller") is True
                or (isinstance(raw_seller, str) and "rozetka" in raw_seller.lower())
            )
            if is_official:
                store_name = "Rozetka.ua" if seller_id is not None else (str(raw_seller).strip() if isinstance(raw_seller, str) and raw_seller.strip() else "Rozetka.ua")
                trust_tier = ChannelTrustTier.TIER_1_OFFICIAL
            elif seller_id is not None and not raw_seller:
                store_name = f"Rozetka Marketplace (#{seller_id})"
                trust_tier = ChannelTrustTier.TIER_2_ESCROW
            else:
                store_name = str(raw_seller or item.get("brand") or "Rozetka Marketplace").strip()
                trust_tier = ChannelTrustTier.TIER_2_ESCROW

            sell_status = str(item.get("sell_status") or "available").lower()
            in_stock = sell_status in ("available", "limited", "in_stock", "1", "true")

            rating_val = item.get("comments_mark") or item.get("rating")
            seller_rating: Optional[float] = None
            if rating_val is not None:
                try:
                    seller_rating = round(float(rating_val), 2)
                except (TypeError, ValueError):
                    seller_rating = None

            reviews_count = 0
            try:
                reviews_count = int(item.get("comments_amount") or 0)
            except (TypeError, ValueError):
                reviews_count = 0

            mpns = extract_model_codes(title)
            for code in mpns:
                if code not in seen_mpns:
                    seen_mpns.add(code)
                    discovered_mpns.append(code)

            prod_obj = item.get("product") if isinstance(item.get("product"), dict) else {}
            state = str(prod_obj.get("state") or item.get("state") or "new").lower()
            if "refurb" in state:
                raw_condition = "Refurbished"
            elif "used" in state or "markdown" in state or item.get("is_markdown"):
                raw_condition = "Уцінка"
            else:
                raw_condition = "Нове"

            condition = ConditionGrader.grade(title, raw_condition=raw_condition)
            landed = self.landed_cost_engine.calculate(
                item_price_raw=price_val,
                currency="UAH",
                route_country="UA",
                weight_kg=task.estimated_weight_kg,
                volumetric_factor=task.volumetric_factor,
                customs_mode=task.customs_mode,
            )

            offers.append(
                NormalizedOffer(
                    offer_id=f"rozetka_{item_id}",
                    task_id=task.task_id,
                    channel=self.channel_name,
                    route_country="UA",
                    store_name=store_name,
                    trust_tier=trust_tier,
                    title=title,
                    normalized_title=normalize_title(title),
                    mpn_codes=mpns,
                    url=href,
                    image_url=img_url,
                    price_raw=price_val,
                    currency="UAH",
                    shipping_raw=0.0,
                    in_stock=in_stock,
                    delivery_days=1 if is_official else 2,
                    warranty_months=24 if is_official else 12,
                    seller_rating=seller_rating,
                    orders_or_reviews=reviews_count,
                    condition=condition,
                    landed_cost=landed,
                    verification_status=VerificationStatus.VERIFIED if grounded else VerificationStatus.MANUAL_CHECK_LINK,
                    raw_price_snippet=raw_price_snippet,
                    is_grounded=grounded,
                    specs={"sell_status": sell_status, "seller_id": seller_id, "state": state},
                )
            )

        if not offers:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="EMPTY_API_RESULT",
                error_note="Rozetka payload contained no valid priced offers",
            )

        return ChannelHarvestResult(
            channel=self.channel_name,
            route_country="UA",
            status=VerificationStatus.VERIFIED,
            offers=offers,
            discovered_mpns=discovered_mpns,
        )

    async def _hydrate_goods_stubs(
        self,
        goods: list[Any],
        headers: dict[str, str],
        max_hydrate: int = 10,
    ) -> list[dict[str, Any]]:
        hydrated: list[dict[str, Any]] = []
        stub_ids: list[str] = []

        for g in goods:
            if not isinstance(g, dict):
                continue
            has_title = bool(g.get("title") or g.get("name"))
            has_price = g.get("price") is not None
            if has_title and has_price:
                hydrated.append(g)
            elif g.get("id") is not None and len(stub_ids) < max_hydrate:
                stub_ids.append(str(g["id"]))

        if not stub_ids:
            return hydrated

        async def _fetch_one(gid: str) -> Optional[dict[str, Any]]:
            url = self.build_product_main_url(gid)
            try:
                sc, bdy, _ = await polite_get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    client=self.client,
                )
                if sc != 200 or is_antibot_challenge(sc, bdy):
                    return None
                parsed = json.loads(bdy)
                item_data = parsed.get("data") if isinstance(parsed, dict) else None
                if isinstance(item_data, dict):
                    item_data.setdefault("id", gid)
                    return item_data
            except Exception:
                return None
            return None

        fetched = await asyncio.gather(*(_fetch_one(gid) for gid in stub_ids))
        for item in fetched:
            if item is not None:
                hydrated.append(item)
        return hydrated

    async def search(
        self,
        task: ShoppingTask,
        *,
        best_ua_uah: Optional[float] = None,
    ) -> ChannelHarvestResult:
        query = self.select_query(task, lang="uk")
        api_url = self.build_api_url(query, category=task.category)
        manual_url = self.build_search_url(query)
        headers = build_browser_headers(referer="https://rozetka.com.ua/")
        headers["Accept"] = "application/json, text/plain, */*"

        try:
            status_code, body, _ = await polite_get(
                api_url,
                headers=headers,
                timeout=self.timeout,
                client=self.client,
            )
        except Exception as exc:
            return self.make_manual_result(
                task=task,
                direct_search_url=manual_url,
                reason_status="HTTP_ERROR",
                best_ua_uah=best_ua_uah,
                error_note=str(exc),
            )

        if is_antibot_challenge(status_code, body):
            return self.make_manual_result(
                task=task,
                direct_search_url=manual_url,
                reason_status=f"ANTIBOT_HTTP_{status_code}",
                best_ua_uah=best_ua_uah,
            )

        try:
            payload = json.loads(body)
        except Exception as exc:
            return self.make_manual_result(
                task=task,
                direct_search_url=manual_url,
                reason_status="NON_JSON_OR_WAF",
                best_ua_uah=best_ua_uah,
                error_note=f"Invalid Rozetka JSON response: {exc}",
            )

        data = payload.get("data") if isinstance(payload, dict) else None
        raw_goods = (data or {}).get("goods") if isinstance(data, dict) else None
        if isinstance(raw_goods, list) and any(
            isinstance(g, dict) and g.get("id") is not None and not ((g.get("title") or g.get("name")) and g.get("price") is not None)
            for g in raw_goods
        ):
            hydrated_goods = await self._hydrate_goods_stubs(raw_goods, headers=headers, max_hydrate=10)
            payload = {"data": {"goods": hydrated_goods}}

        result = self.parse_api_payload(payload, task, search_url=manual_url)
        if result.manual_link is not None and best_ua_uah:
            return self.make_manual_result(
                task=task,
                direct_search_url=manual_url,
                reason_status=result.manual_link.reason_status,
                best_ua_uah=best_ua_uah,
                error_note=result.error_note,
            )
        return result
