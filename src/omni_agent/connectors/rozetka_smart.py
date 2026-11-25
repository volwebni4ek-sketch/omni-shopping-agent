from __future__ import annotations

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


class RozetkaConnector(BaseConnector):
    channel_name = "rozetka"
    default_route_country = "UA"
    default_currency = "UAH"

    def build_search_url(self, query: str) -> str:
        encoded = urllib.parse.quote(query)
        return f"https://rozetka.com.ua/ua/search/?text={encoded}&seller=rozetka"

    def build_api_url(self, query: str) -> str:
        encoded = urllib.parse.quote(query)
        return f"https://search.rozetka.com.ua/ua/search/api/v6/?front-type=xl&country=UA&lang=ua&text={encoded}"

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
            seller_name = str(item.get("seller") or item.get("brand") or "Rozetka").strip()
            is_official = "rozetka" in seller_name.lower() or item.get("is_rozetka_seller") is True
            trust_tier = ChannelTrustTier.TIER_1_OFFICIAL if is_official else ChannelTrustTier.TIER_2_ESCROW

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

            condition = ConditionGrader.grade(title, raw_condition="Нове")
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
                    store_name=seller_name or "Rozetka",
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
                    specs={"sell_status": sell_status},
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

    async def search(
        self,
        task: ShoppingTask,
        *,
        best_ua_uah: Optional[float] = None,
    ) -> ChannelHarvestResult:
        query = self.select_query(task, lang="uk")
        api_url = self.build_api_url(query)
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
