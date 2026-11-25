from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
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
from omni_agent.core.landed_cost import LandedCostEngine
from omni_agent.core.models import (
    ChannelTrustTier,
    NormalizedOffer,
    ShoppingTask,
    VerificationStatus,
)
from omni_agent.core.normalizer import extract_model_codes, normalize_title


POSSIBLE_CHROME_PATHS = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


class OLXConnector(BaseConnector):
    channel_name = "olx"
    default_route_country = "UA"
    default_currency = "UAH"

    def __init__(
        self,
        *,
        use_headless_chrome: bool = False,
        olx_escrow_fee_uah: float = 120.0,
        landed_cost_engine: Optional[LandedCostEngine] = None,
        timeout: float = 15.0,
        client=None,
    ) -> None:
        super().__init__(landed_cost_engine=landed_cost_engine, timeout=timeout, client=client)
        self.use_headless_chrome = use_headless_chrome
        self.olx_escrow_fee_uah = olx_escrow_fee_uah
        self.chrome_path = self._find_chrome()

    @staticmethod
    def _find_chrome() -> Optional[str]:
        for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
            found = shutil.which(name)
            if found:
                return found
        for path in POSSIBLE_CHROME_PATHS:
            if os.path.exists(path):
                return path
        return None

    def build_search_url(self, query: str, region_slug: str = "") -> str:
        clean_q = re.sub(r"[^\w\s-]", "", query, flags=re.UNICODE).strip()
        slug = re.sub(r"\s+", "-", clean_q)
        region_clean = (region_slug or "").strip().strip("/")
        if region_clean:
            return f"https://www.olx.ua/uk/list/{region_clean}/q-{slug}/"
        return f"https://www.olx.ua/uk/list/q-{slug}/"

    @staticmethod
    def _estimate_olx_escrow_fee(price_uah: float, base_fee_uah: float) -> float:
        # OLX Delivery typically adds 1.5% + ~20 UAH + Nova Poshta ~100 UAH
        return round(max(base_fee_uah, price_uah * 0.015 + 100.0), 2)

    def _dump_dom_sync(self, url: str) -> str:
        if not self.chrome_path:
            return ""
        cmd = [
            self.chrome_path,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--dump-dom",
            url,
        ]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=int(self.timeout),
            )
            if proc.returncode == 0 and len(proc.stdout) > 500:
                return proc.stdout
        except Exception:
            return ""
        return ""

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
                reason_status="ANTIBOT_OR_AWS_WAF",
                error_note="OLX HTML triggered AWS WAF / anti-bot detection",
            )

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all(attrs={"data-cy": "l-card"})

        offers: list[NormalizedOffer] = []
        discovered_mpns: list[str] = []
        seen_mpns: set[str] = set()
        seen_urls: set[str] = set()

        for idx, card in enumerate(cards):
            strings = list(card.stripped_strings)
            if not strings:
                continue

            a = card.find("a", href=True)
            href = (a.get("href") or "").strip() if a else ""
            if not href:
                continue
            full_url = f"https://www.olx.ua{href}" if href.startswith("/") else href
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            h = card.find(["h4", "h6", "h3"])
            title = h.get_text(" ", strip=True) if h else strings[0]
            if len(title) < 3:
                continue

            img = card.find("img")
            img_url = ""
            if img:
                img_url = (img.get("src") or img.get("data-src") or "").strip()

            raw_price_snippet = ""
            price_val: Optional[float] = None
            condition_raw = ""
            location = ""

            for s in strings:
                s_clean = s.replace("\xa0", " ").replace("\u202f", " ").strip()
                s_low = s_clean.lower()
                if ("грн" in s_low or "₴" in s_clean) and re.search(r"\d", s_clean):
                    parsed = parse_price_value(s_clean)
                    if parsed is not None and parsed > 50:
                        price_val = parsed
                        raw_price_snippet = s_clean
                elif s_low in ("нове", "новий", "нова"):
                    condition_raw = "Нове"
                elif s_low in ("вживане", "б/в", "б/у", "вживаний"):
                    condition_raw = "Вживане"
                elif " - " in s_clean:
                    location = s_clean

            if price_val is None or not raw_price_snippet:
                continue

            grounded, _ = ComplianceManager.verify_price_grounding(price_val, raw_price_snippet)
            condition = ConditionGrader.grade(
                title,
                description=" ".join(strings),
                raw_condition=condition_raw,
            )

            mpns = extract_model_codes(title)
            for code in mpns:
                if code not in seen_mpns:
                    seen_mpns.add(code)
                    discovered_mpns.append(code)

            escrow_fee = self._estimate_olx_escrow_fee(price_val, self.olx_escrow_fee_uah)
            landed = self.landed_cost_engine.calculate(
                item_price_raw=price_val,
                currency="UAH",
                route_country="UA",
                weight_kg=task.estimated_weight_kg,
                volumetric_factor=task.volumetric_factor,
                customs_mode=task.customs_mode,
                local_escrow_shipping_uah=escrow_fee,
            )

            match_id = re.search(r"ID([a-zA-Z0-9]+)", href)
            prod_id = (
                f"olx_{match_id.group(1)}"
                if match_id
                else f"olx_{hashlib.sha1(full_url.encode('utf-8')).hexdigest()[:10]}_{idx}"
            )

            offers.append(
                NormalizedOffer(
                    offer_id=prod_id,
                    task_id=task.task_id,
                    channel=self.channel_name,
                    route_country="UA",
                    store_name=f"OLX ({location})" if location else "Продавець OLX",
                    trust_tier=ChannelTrustTier.TIER_2_ESCROW,
                    title=title,
                    normalized_title=normalize_title(title),
                    mpn_codes=mpns,
                    url=full_url,
                    image_url=img_url,
                    price_raw=price_val,
                    currency="UAH",
                    shipping_raw=0.0,
                    in_stock=True,
                    delivery_days=3,
                    warranty_months=0,
                    condition=condition,
                    landed_cost=landed,
                    verification_status=VerificationStatus.VERIFIED if grounded else VerificationStatus.MANUAL_CHECK_LINK,
                    raw_price_snippet=raw_price_snippet,
                    is_grounded=grounded,
                    specs={"location": location, "escrow": "OLX Доставка"},
                )
            )

        if not offers:
            return self.make_manual_result(
                task=task,
                direct_search_url=direct_url,
                reason_status="EMPTY_DOM_OR_AWS_WAF",
                error_note="OLX returned no parseable l-card items",
            )

        return ChannelHarvestResult(
            channel=self.channel_name,
            route_country="UA",
            status=VerificationStatus.VERIFIED,
            offers=offers,
            discovered_mpns=discovered_mpns,
        )

    @staticmethod
    def _infer_region_slugs(task: ShoppingTask) -> list[str]:
        if not task.require_local_pickup or not task.pickup_regions:
            return [""]
        regions_low = [r.lower().strip() for r in task.pickup_regions if r and r.strip()]
        has_kyiv = any(
            k in r for r in regions_low for k in ("київ", "kyiv", "kiev", "бровар", "ірпін", "буча", "бориспіль", "вишнев")
        )
        has_zhytomyr = any("житомир" in r or "zhytomyr" in r for r in regions_low)
        slugs: list[str] = []
        if has_kyiv:
            slugs.append("ko")
        if has_zhytomyr:
            slugs.append("zhitomirskaya-oblast")
        return slugs or [""]

    async def _fetch_single_search(
        self,
        url: str,
        task: ShoppingTask,
        *,
        best_ua_uah: Optional[float] = None,
    ) -> ChannelHarvestResult:
        html = ""
        if self.use_headless_chrome and self.chrome_path:
            html = await asyncio.to_thread(self._dump_dom_sync, url)
            if not html or is_antibot_challenge(200, html):
                return self.make_manual_result(
                    task=task,
                    direct_search_url=url,
                    reason_status="ANTIBOT_OR_AWS_WAF",
                    best_ua_uah=best_ua_uah,
                    error_note="OLX headless DOM dump blocked or empty",
                )
        else:
            try:
                status_code, html, final_url = await polite_get(
                    url,
                    timeout=self.timeout,
                    client=self.client,
                    referer="https://www.olx.ua/",
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

    async def search(
        self,
        task: ShoppingTask,
        *,
        best_ua_uah: Optional[float] = None,
    ) -> ChannelHarvestResult:
        query = self.select_query(task, lang="uk")
        region_slugs = self._infer_region_slugs(task)

        if len(region_slugs) == 1:
            url = self.build_search_url(query, region_slug=region_slugs[0])
            return await self._fetch_single_search(url, task, best_ua_uah=best_ua_uah)

        urls = [self.build_search_url(query, region_slug=rs) for rs in region_slugs]
        results = await asyncio.gather(
            *(self._fetch_single_search(u, task, best_ua_uah=best_ua_uah) for u in urls)
        )

        merged_offers: list[NormalizedOffer] = []
        seen_offer_ids: set[str] = set()
        seen_urls: set[str] = set()
        discovered_mpns: list[str] = []
        seen_mpns: set[str] = set()
        first_manual: Optional[ChannelHarvestResult] = None

        for res in results:
            if res.manual_link is not None and first_manual is None:
                first_manual = res
            for code in res.discovered_mpns:
                if code not in seen_mpns:
                    seen_mpns.add(code)
                    discovered_mpns.append(code)
            for off in res.offers:
                if off.offer_id not in seen_offer_ids and off.url not in seen_urls:
                    seen_offer_ids.add(off.offer_id)
                    seen_urls.add(off.url)
                    merged_offers.append(off)

        if merged_offers:
            return ChannelHarvestResult(
                channel=self.channel_name,
                route_country="UA",
                status=VerificationStatus.VERIFIED,
                offers=merged_offers,
                discovered_mpns=discovered_mpns,
            )

        if first_manual is not None:
            return first_manual

        return self.make_manual_result(
            task=task,
            direct_search_url=urls[0],
            reason_status="EMPTY_DOM_OR_AWS_WAF",
            best_ua_uah=best_ua_uah,
            error_note="OLX regional searches returned no parseable items",
        )
