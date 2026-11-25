from __future__ import annotations

import html as html_lib
import re
from typing import Optional

from pydantic import BaseModel, Field

from omni_agent.connectors.base import is_antibot_challenge, polite_get
from omni_agent.core.models import Coupon

_MSG_BLOCK_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_ALI_LINK_RE = re.compile(
    r"https?://(?:s\.click\.aliexpress\.com|a\.aliexpress\.com|(?:www\.)?aliexpress\.com)/[^\s\"'<>]+",
    re.IGNORECASE,
)
# Matches patterns like "$5 off $30", "5$ від 30$", "-$10 на замовлення від $80"
_THRESHOLD_RE = re.compile(
    r"(?:\$|USD\s*)?(\d+(?:[.,]\d+)?)\s*(?:\$|USD)?\s*(?:off|від|from|на|при замовленні від|/)\s*(?:\$|USD\s*)?(\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)
_CODE_LINE_RE = re.compile(
    r"(?:промокод|код|code|coupon|купон)[:\s\-–—]*([A-Z0-9]{4,16})\b",
    re.IGNORECASE,
)


class TelegramDealPost(BaseModel):
    channel: str
    text: str
    links: list[str] = Field(default_factory=list)
    coupons: list[Coupon] = Field(default_factory=list)


def parse_telegram_channel_html(raw_html: str, channel: str = "unknown") -> list[TelegramDealPost]:
    """Extract deal posts, AliExpress links, and promo codes from public t.me/s/<channel> HTML."""
    posts: list[TelegramDealPost] = []
    for match in _MSG_BLOCK_RE.finditer(raw_html):
        block = match.group(1)
        links = _ALI_LINK_RE.findall(block)
        clean_text = html_lib.unescape(_TAG_STRIP_RE.sub(" ", block))
        clean_text = re.sub(r"\s+", " ", clean_text).strip()
        if not clean_text:
            continue

        codes = _CODE_LINE_RE.findall(clean_text)
        thresholds = _THRESHOLD_RE.findall(clean_text)

        coupons: list[Coupon] = []
        if codes and thresholds:
            disc_str, min_str = thresholds[0]
            disc = float(disc_str.replace(",", "."))
            min_sp = float(min_str.replace(",", "."))
            for code in dict.fromkeys(c.upper() for c in codes):
                coupons.append(
                    Coupon(
                        code=code,
                        description=f"{code}: ${disc:g} off ${min_sp:g} (from @{channel})",
                        discount_amount=disc,
                        min_spend=min_sp,
                        currency="USD",
                    )
                )
        elif codes:
            for code in dict.fromkeys(c.upper() for c in codes):
                coupons.append(
                    Coupon(
                        code=code,
                        description=f"Promo code {code} (from @{channel})",
                        currency="USD",
                    )
                )

        if links or coupons:
            posts.append(
                TelegramDealPost(
                    channel=channel,
                    text=clean_text[:500],
                    links=list(dict.fromkeys(links)),
                    coupons=coupons,
                )
            )
    return posts


class TelegramPromoScraper:
    """Scrapes public Telegram deal channels via https://t.me/s/<channel> without API keys."""

    def __init__(self, channels: Optional[list[str]] = None, timeout: float = 15.0, client=None) -> None:
        self.channels = channels or []
        self.timeout = timeout
        self.client = client

    async def fetch_channel_posts(self, channel: str) -> list[TelegramDealPost]:
        clean_channel = channel.lstrip("@").strip()
        if not clean_channel:
            return []
        url = f"https://t.me/s/{clean_channel}"
        status_code, html, _ = await polite_get(url, timeout=self.timeout, client=self.client)
        if status_code != 200 or is_antibot_challenge(status_code, html):
            return []
        return parse_telegram_channel_html(html, channel=clean_channel)

    async def collect_coupons(self, channels: Optional[list[str]] = None) -> list[Coupon]:
        target_channels = channels or self.channels
        all_coupons: list[Coupon] = []
        seen_keys: set[tuple[Optional[str], float, float]] = set()
        for ch in target_channels:
            try:
                posts = await self.fetch_channel_posts(ch)
            except Exception:
                continue
            for post in posts:
                for coupon in post.coupons:
                    key = (coupon.code, coupon.discount_amount, coupon.min_spend)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_coupons.append(coupon)
        return all_coupons
