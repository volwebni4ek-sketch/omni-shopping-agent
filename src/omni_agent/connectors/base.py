from __future__ import annotations

import os
import random
import re
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

from omni_agent.core.compliance import ComplianceManager
from omni_agent.core.landed_cost import LandedCostEngine
from omni_agent.core.models import (
    NormalizedOffer,
    ShoppingTask,
    VerificationStatus,
)

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
]

STRONG_ANTIBOT_MARKERS: tuple[str, ...] = (
    "cf-turnstile",
    "x5sec",
    "api-services-support@amazon.com",
    "datadome",
    "validatecaptcha",
    "_____tmd_____",
    "challenge-platform",
    "cf-challenge",
    "aws-waf",
    "awswaf",
)


class ManualCheckLink(BaseModel):
    task_id: str
    channel: str
    route_country: str
    reason_status: str
    breakeven_max_foreign_price: Optional[float] = None
    currency: str = "UAH"
    breakeven_hint_uk: str = ""
    direct_search_url: str


class ChannelHarvestResult(BaseModel):
    channel: str
    route_country: str
    status: VerificationStatus
    offers: list[NormalizedOffer] = Field(default_factory=list)
    discovered_mpns: list[str] = Field(default_factory=list)
    manual_link: Optional[ManualCheckLink] = None
    error_note: str = ""


def is_antibot_challenge(status_code: int, html: str = "") -> bool:
    """
    Fail-closed anti-bot / WAF / CAPTCHA detector.
    Returns True immediately on 403/429/503, strong challenge markers, or
    compact interstitial pages containing generic captcha/punish prompts.
    Avoids false positives on large SSR pages (e.g. Hotline/Prom > 80KB) that
    merely reference '/svc/recaptcha' or feature flags in JS config.
    """
    if status_code in (403, 429, 503):
        return True
    if not html:
        return False
    lower = html.lower()
    if any(marker in lower for marker in STRONG_ANTIBOT_MARKERS):
        return True
    if "punish" in lower and ("captcha" in lower or "rgv587_flag" in lower):
        return True
    # Generic 'captcha' on compact challenge interstitials (< 60 KB) without real product DOM
    if len(html) < 60_000 and "captcha" in lower:
        has_product_dom = (
            'data-qaid="product_block"' in lower
            or 'class="list-item' in lower
            or 'data-component-type="s-search-result"' in lower
            or 'data-cy="l-card"' in lower
        )
        if not has_product_dom:
            return True
    return False


def build_browser_headers(
    referer: Optional[str] = None,
    accept_language: str = "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
) -> dict[str, str]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": accept_language,
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _sanitize_no_proxy_env() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        val = os.environ.get(var, "")
        if "::1" in val:
            os.environ[var] = ",".join(p.strip() for p in val.split(",") if "::1" not in p)


async def polite_get(
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    cookies: Optional[dict[str, str]] = None,
    timeout: float = 15.0,
    client: Optional[httpx.AsyncClient] = None,
    referer: Optional[str] = None,
    accept_language: str = "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
) -> tuple[int, str, str]:
    """
    Polite async HTTP GET that never attempts to bypass anti-bot challenges
    and returns (status_code, body_text, final_url) cleanly.
    """
    _sanitize_no_proxy_env()
    req_headers = headers or build_browser_headers(referer=referer, accept_language=accept_language)

    if client is not None:
        final_headers = dict(req_headers)
        if cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            existing = final_headers.get("Cookie", "")
            final_headers["Cookie"] = f"{existing}; {cookie_str}".strip("; ")
        resp = await client.get(url, params=params, headers=final_headers)
        return resp.status_code, resp.text, str(resp.url)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=req_headers,
        cookies=cookies,
    ) as local_client:
        resp = await local_client.get(url, params=params)
        return resp.status_code, resp.text, str(resp.url)


def parse_price_value(raw_text: str) -> Optional[float]:
    """
    Parse first numeric price from localized UA/EU/US price strings
    such as '14 299 – 16 500 ₴', 'ab 599,00 €', 'od 2 499,00 zł', '$1,299.99'.
    """
    if not raw_text:
        return None
    cleaned = raw_text.replace("\u00a0", " ").replace("\u202f", " ").strip()
    match = re.search(r"\d+(?:[\s.,']\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?", cleaned)
    if not match:
        return None
    return ComplianceManager._parse_numeric_token(match.group(0))


def parse_price_range(raw_text: str) -> tuple[Optional[float], Optional[float]]:
    """Parse min and optional max price from range strings like '14 299 – 16 500 ₴'."""
    if not raw_text:
        return None, None
    cleaned = raw_text.replace("\u00a0", " ").replace("\u202f", " ").strip()
    matches = re.findall(r"\d+(?:[\s.,']\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?", cleaned)
    values: list[float] = []
    for tok in matches:
        val = ComplianceManager._parse_numeric_token(tok)
        if val is not None and val > 0:
            values.append(val)
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], None
    return min(values[0], values[1]), max(values[0], values[1])


def build_manual_check_link(
    *,
    task: ShoppingTask,
    channel: str,
    route_country: str,
    currency: str,
    direct_search_url: str,
    reason_status: str,
    best_ua_uah: Optional[float] = None,
    direct_to_ua_export: bool = False,
    shipping_raw: float = 0.0,
    landed_cost_engine: Optional[LandedCostEngine] = None,
) -> ManualCheckLink:
    route = (route_country or "UA").upper()
    cur = (currency or "UAH").upper()
    engine = landed_cost_engine or LandedCostEngine()

    reference_ua_uah = best_ua_uah
    if reference_ua_uah is None or reference_ua_uah <= 0:
        reference_ua_uah = task.target_price_uah or task.max_budget_uah

    breakeven_price: Optional[float] = None
    hint_uk = ""

    if route == "UA":
        if reference_ua_uah is not None and reference_ua_uah > 0:
            breakeven_price = round(reference_ua_uah, 2)
            hint_uk = (
                f"Орієнтир ціни ({channel}): до {breakeven_price:.2f} {cur} "
                f"(цільовий бюджет {round(reference_ua_uah)} ₴)"
            )
        else:
            hint_uk = f"Перевірте наявність та ціну на {channel} вручну за прямим посиланням"
    else:
        if reference_ua_uah is not None and reference_ua_uah > 0:
            be = engine.calculate_foreign_breakeven_price(
                best_ua_uah=reference_ua_uah,
                route_country=route,
                currency=cur,
                weight_kg=task.estimated_weight_kg,
                volumetric_factor=task.volumetric_factor,
                customs_mode=task.customs_mode,
                direct_to_ua_export=direct_to_ua_export,
                shipping_raw=shipping_raw,
            )
            breakeven_price = float(be["max_foreign_price"])
            hint_uk = str(be["hint_uk"])
        else:
            hint_uk = (
                f"Перевірте актуальну ціну на {channel} ({route}, {cur}) вручну — "
                f"доступ обмежено антибот-захистом"
            )

    return ManualCheckLink(
        task_id=task.task_id,
        channel=channel,
        route_country=route,
        reason_status=reason_status,
        breakeven_max_foreign_price=breakeven_price,
        currency=cur,
        breakeven_hint_uk=hint_uk,
        direct_search_url=direct_search_url,
    )


class BaseConnector:
    channel_name: str = "base"
    default_route_country: str = "UA"
    default_currency: str = "UAH"

    def __init__(
        self,
        *,
        landed_cost_engine: Optional[LandedCostEngine] = None,
        timeout: float = 15.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.landed_cost_engine = landed_cost_engine or LandedCostEngine()
        self.timeout = timeout
        self.client = client

    @staticmethod
    def select_query(task: ShoppingTask, lang: Optional[str] = None, prefer_mpn: bool = False) -> str:
        if prefer_mpn and task.canonical_mpns:
            candidate = task.canonical_mpns[0]
        elif lang and task.queries_i18n.get(lang):
            candidate = task.queries_i18n[lang]
        elif task.raw_query:
            candidate = task.raw_query
        elif task.canonical_mpns:
            candidate = task.canonical_mpns[0]
        else:
            candidate = ""
        return ComplianceManager.sanitize_gdpr_query(candidate)

    def make_manual_result(
        self,
        *,
        task: ShoppingTask,
        direct_search_url: str,
        reason_status: str,
        route_country: Optional[str] = None,
        currency: Optional[str] = None,
        best_ua_uah: Optional[float] = None,
        direct_to_ua_export: bool = False,
        shipping_raw: float = 0.0,
        error_note: str = "",
    ) -> ChannelHarvestResult:
        route = (route_country or self.default_route_country).upper()
        cur = (currency or self.default_currency).upper()
        manual_link = build_manual_check_link(
            task=task,
            channel=self.channel_name,
            route_country=route,
            currency=cur,
            direct_search_url=direct_search_url,
            reason_status=reason_status,
            best_ua_uah=best_ua_uah,
            direct_to_ua_export=direct_to_ua_export,
            shipping_raw=shipping_raw,
            landed_cost_engine=self.landed_cost_engine,
        )
        return ChannelHarvestResult(
            channel=self.channel_name,
            route_country=route,
            status=VerificationStatus.MANUAL_CHECK_LINK,
            offers=[],
            discovered_mpns=[],
            manual_link=manual_link,
            error_note=error_note or reason_status,
        )
