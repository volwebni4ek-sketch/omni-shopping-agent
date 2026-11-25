#!/usr/bin/env python3
"""
Omni Shopping Agent — Zero-Dependency Standalone Residential IP Relay Harvester.

Runs on ANY Windows / macOS / Linux machine with Python 3.9+ installed.
Uses ONLY Python 3 standard library modules (ZERO pip/uv dependencies).

Safety & Fail-Closed Compliance:
  - Never bypasses, solves, clicks through, or waits out CAPTCHAs / Cloudflare / AWS WAF.
  - On HTTP 403/429/503 or anti-bot challenge markers, immediately records
    `MANUAL_CHECK_LINK` with a direct search URL and moves on cleanly.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import html as html_lib
import json
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
]

STRONG_ANTIBOT_MARKERS = (
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

ROZETKA_CATEGORY_SECTION_MAP = {
    "monitors_large_format": "80089",
    "ai_mini_pc": "80095",
}

POSSIBLE_CHROME_PATHS = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

_GENERIC_SEARCH_URL_PATTERNS = (
    "/search",
    "?q=",
    "&q=",
    "?text=",
    "&text=",
    "search_term=",
    "/sr/?",
    "/list/q-",
    "/q-",
    "/s?k=",
    "/w/wholesale-",
)
_ROZETKA_PRODUCT_RE = re.compile(r"/p\d+(?:/|$|\?)")
_PROM_PRODUCT_RE = re.compile(r"/(?:ua/)?[mp]-?[0-9a-zA-Z_-]+.*\.html")
_HOTLINE_NUMERIC_CAT_RE = re.compile(r"/\d+/?(?:\?.*)?$")
_HOTLINE_SLUG_WITH_LETTERS_RE = re.compile(r"/[a-zA-Z0-9_-]*[a-zA-Z][a-zA-Z0-9_-]*/?(?:\?.*)?$")


# ---------------------------------------------------------------------------
# Lightweight Stdlib DOM Tree Builder
# ---------------------------------------------------------------------------

class MiniNode:
    """Minimal DOM node built via stdlib html.parser."""

    def __init__(self, tag: str, attrs: Optional[dict[str, str]] = None) -> None:
        self.tag = tag.lower()
        self.attrs: dict[str, str] = attrs or {}
        self.children: list[MiniNode] = []
        self.text_chunks: list[str] = []
        self._order: list[tuple[str, Any]] = []

    def add_text(self, txt: str) -> None:
        if txt:
            self.text_chunks.append(txt)
            self._order.append(("t", txt))

    def add_child(self, node: "MiniNode") -> None:
        self.children.append(node)
        self._order.append(("c", node))

    def attr(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    def classes(self) -> set[str]:
        raw = self.attrs.get("class", "")
        return {c.strip() for c in raw.split() if c.strip()}

    def has_class(self, cls_name: str) -> bool:
        return cls_name in self.classes()

    def iter_nodes(self) -> list["MiniNode"]:
        out: list[MiniNode] = []
        stack = list(reversed(self.children))
        while stack:
            node = stack.pop()
            out.append(node)
            if node.children:
                stack.extend(reversed(node.children))
        return out

    def find_all(
        self,
        tags: Optional[str | tuple[str, ...]] = None,
        attrs: Optional[dict[str, str]] = None,
        class_contains: Optional[str] = None,
        predicate: Optional[Any] = None,
    ) -> list["MiniNode"]:
        if isinstance(tags, str):
            tag_set: Optional[set[str]] = {tags.lower()}
        elif tags is not None:
            tag_set = {t.lower() for t in tags}
        else:
            tag_set = None

        matched: list[MiniNode] = []
        for node in self.iter_nodes():
            if tag_set is not None and node.tag not in tag_set:
                continue
            if attrs:
                ok = True
                for k, v in attrs.items():
                    if node.attrs.get(k) != v:
                        ok = False
                        break
                if not ok:
                    continue
            if class_contains is not None:
                cls_raw = node.attrs.get("class", "")
                if class_contains not in cls_raw:
                    continue
            if predicate is not None and not predicate(node):
                continue
            matched.append(node)
        return matched

    def find(
        self,
        tags: Optional[str | tuple[str, ...]] = None,
        attrs: Optional[dict[str, str]] = None,
        class_contains: Optional[str] = None,
        predicate: Optional[Any] = None,
    ) -> Optional["MiniNode"]:
        res = self.find_all(tags=tags, attrs=attrs, class_contains=class_contains, predicate=predicate)
        return res[0] if res else None

    def get_text(self, separator: str = " ", strip: bool = True) -> str:
        parts: list[str] = []
        self._collect_text(parts)
        joined = separator.join(parts)
        joined = re.sub(r"\s+", " ", joined)
        return joined.strip() if strip else joined

    def _collect_text(self, out: list[str]) -> None:
        for kind, val in self._order:
            if kind == "t":
                cleaned = val.strip()
                if cleaned:
                    out.append(cleaned)
            else:
                if val.tag not in ("script", "style"):
                    val._collect_text(out)

    def stripped_strings(self) -> list[str]:
        out: list[str] = []
        for kind, val in self._order:
            if kind == "t":
                s = re.sub(r"\s+", " ", val).strip()
                if s:
                    out.append(s)
            else:
                if val.tag not in ("script", "style"):
                    out.extend(val.stripped_strings())
        return out


class MiniHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = MiniNode("document")
        self.stack: list[MiniNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attr_dict = {k.lower(): (v if v is not None else "") for k, v in attrs}
        node = MiniNode(t, attr_dict)
        self.stack[-1].add_child(node)
        if t not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attr_dict = {k.lower(): (v if v is not None else "") for k, v in attrs}
        node = MiniNode(t, attr_dict)
        self.stack[-1].add_child(node)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == t:
                del self.stack[idx:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].add_text(data)


def parse_dom(html_text: str) -> MiniNode:
    parser = MiniHTMLParser()
    parser.feed(html_text or "")
    parser.close()
    return parser.root


# ---------------------------------------------------------------------------
# Helpers: Price Parsing, Anti-Bot Detection, Direct URL Verification
# ---------------------------------------------------------------------------

def is_antibot_challenge(status_code: int, body: str = "") -> bool:
    if status_code in (403, 429, 503):
        return True
    if not body:
        return False
    lower = body.lower()
    if any(marker in lower for marker in STRONG_ANTIBOT_MARKERS):
        return True
    if "punish" in lower and ("captcha" in lower or "rgv587_flag" in lower):
        return True
    if len(body) < 60_000 and "captcha" in lower:
        has_dom = (
            'data-qaid="product_block"' in lower
            or 'class="list-item' in lower
            or 'data-component-type="s-search-result"' in lower
            or 'data-cy="l-card"' in lower
        )
        if not has_dom:
            return True
    return False


def _parse_numeric_token(token: str) -> Optional[float]:
    s = token.strip().replace(" ", "").replace("'", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        if len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) >= 1:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_price_value(raw_text: str) -> Optional[float]:
    if not raw_text:
        return None
    cleaned = raw_text.replace("\u00a0", " ").replace("\u202f", " ").strip()
    match = re.search(r"\d+(?:[\s.,']\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?", cleaned)
    if not match:
        return None
    return _parse_numeric_token(match.group(0))


def parse_price_range(raw_text: str) -> tuple[Optional[float], Optional[float]]:
    if not raw_text:
        return None, None
    cleaned = raw_text.replace("\u00a0", " ").replace("\u202f", " ").strip()
    matches = re.findall(r"\d+(?:[\s.,']\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?", cleaned)
    vals: list[float] = []
    for tok in matches:
        v = _parse_numeric_token(tok)
        if v is not None and v > 0:
            vals.append(v)
    if not vals:
        return None, None
    if len(vals) == 1:
        return vals[0], None
    return min(vals[0], vals[1]), max(vals[0], vals[1])


def is_direct_product_url(url: str, channel: str = "") -> bool:
    if not url or not isinstance(url, str):
        return False
    u_clean = url.strip()
    if not u_clean:
        return False
    u_low = u_clean.lower()
    if any(pat in u_low for pat in _GENERIC_SEARCH_URL_PATTERNS):
        return False
    ch = (channel or "").strip().lower()
    if ch == "rozetka":
        return bool(_ROZETKA_PRODUCT_RE.search(u_low))
    if ch == "olx":
        return "/d/" in u_low or "/obyavlenie/" in u_low
    if ch == "prom":
        return bool(_PROM_PRODUCT_RE.search(u_low))
    if ch == "hotline":
        if "/sr/" in u_low:
            return False
        path = re.sub(r"^https?://[^/]+", "", u_low).split("#")[0].split("?")[0]
        if not path or path == "/" or _HOTLINE_NUMERIC_CAT_RE.search(path):
            return False
        return bool(_HOTLINE_SLUG_WITH_LETTERS_RE.search(path))
    if ch == "aliexpress":
        return "/item/" in u_low
    if ch.startswith("amazon"):
        return "/dp/" in u_low or "/gp/product/" in u_low
    if ch == "idealo_de":
        return "/offersofproduct/" in u_low or "idealo.de/preisvergleich/" in u_low
    if ch == "ceneo_pl":
        return ";szukaj-" not in u_low and bool(re.search(r"ceneo\.pl/\d+", u_low))
    return True


def _sanitize_query(query: str) -> str:
    if not query:
        return ""
    s = re.sub(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", " ", query)
    s = re.sub(r"(?:\+?\d[\s\-\(\)]*){9,15}", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _select_query(task: dict[str, Any], lang: str = "uk", prefer_mpn: bool = False) -> str:
    mpns = task.get("canonical_mpns") or []
    i18n = task.get("queries_i18n") or {}
    if prefer_mpn and mpns:
        q = str(mpns[0])
    elif lang and i18n.get(lang):
        q = str(i18n[lang])
    elif task.get("raw_query"):
        q = str(task["raw_query"])
    elif mpns:
        q = str(mpns[0])
    else:
        q = ""
    return _sanitize_query(q)


def _find_chrome_path() -> Optional[str]:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    for path in POSSIBLE_CHROME_PATHS:
        if os.path.exists(path):
            return path
    return None


def _dump_dom_with_chrome(chrome_path: str, url: str, timeout: float = 20.0) -> str:
    if not chrome_path:
        return ""
    cmd = [
        chrome_path,
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
            timeout=int(max(5, timeout)),
        )
        if proc.returncode == 0 and len(proc.stdout) > 500:
            return proc.stdout
    except Exception:
        return ""
    return ""


def _decode_http_bytes(raw: bytes, content_encoding: str, content_type: str) -> str:
    enc_low = (content_encoding or "").lower()
    data = raw
    try:
        if "gzip" in enc_low:
            data = gzip.decompress(raw)
        elif "deflate" in enc_low:
            data = zlib.decompress(raw)
    except Exception:
        data = raw

    charset = "utf-8"
    m_cs = re.search(r"charset=([a-zA-Z0-9_\-]+)", content_type or "", re.IGNORECASE)
    if m_cs:
        charset = m_cs.group(1)
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def http_get(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    cookies: Optional[dict[str, str]] = None,
    referer: Optional[str] = None,
    accept_language: str = "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    timeout: float = 15.0,
) -> tuple[int, str, str]:
    req_headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": accept_language,
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if referer:
        req_headers["Referer"] = referer
    if headers:
        req_headers.update(headers)
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req_headers["Cookie"] = cookie_str

    req = urllib.request.Request(url, headers=req_headers, method="GET")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read()
            ce = resp.headers.get("Content-Encoding", "")
            ct = resp.headers.get("Content-Type", "")
            final_url = resp.geturl()
            return int(status), _decode_http_bytes(raw, ce, ct), str(final_url)
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        ce = exc.headers.get("Content-Encoding", "") if exc.headers else ""
        ct = exc.headers.get("Content-Type", "") if exc.headers else ""
        return int(exc.code), _decode_http_bytes(raw, ce, ct), str(exc.geturl() or url)


def _make_manual_link(
    task: dict[str, Any],
    *,
    channel: str,
    route_country: str,
    currency: str,
    reason_status: str,
    direct_search_url: str,
    note: str = "",
) -> dict[str, Any]:
    target_uah = task.get("target_price_uah") or task.get("max_budget_uah")
    if route_country == "UA" and target_uah:
        hint = f"Орієнтир ціни ({channel}): до {float(target_uah):.0f} UAH за прямим посиланням"
    elif target_uah:
        hint = (
            f"Перевірте {channel} ({route_country}, {currency}) вручну — "
            f"цільовий бюджет у UA ≤ {float(target_uah):.0f} ₴"
        )
    else:
        hint = f"Перевірте наявність та ціну на {channel} ({route_country}) вручну за прямим посиланням"
    if note:
        hint = f"{hint} [{note}]"
    return {
        "task_id": task.get("task_id", ""),
        "channel": channel,
        "route_country": route_country,
        "reason_status": reason_status,
        "breakeven_max_foreign_price": float(target_uah) if (route_country == "UA" and target_uah) else None,
        "currency": currency,
        "breakeven_hint_uk": hint,
        "direct_search_url": direct_search_url,
    }


# ---------------------------------------------------------------------------
# Channel Parsers & Harvesters
# ---------------------------------------------------------------------------

def _infer_olx_region_slugs(task: dict[str, Any]) -> list[str]:
    explicit = task.get("olx_regions")
    if isinstance(explicit, list) and explicit:
        return [str(r).strip().strip("/") for r in explicit if str(r).strip()]
    regions = task.get("pickup_regions") or []
    if not regions and not task.get("require_local_pickup"):
        return [""]
    regions_low = [str(r).lower().strip() for r in regions if r and str(r).strip()]
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


def build_olx_search_url(query: str, region_slug: str = "") -> str:
    clean_q = re.sub(r"[^\w\s-]", "", query, flags=re.UNICODE).strip()
    slug = re.sub(r"\s+", "-", clean_q)
    rc = (region_slug or "").strip().strip("/")
    if rc:
        return f"https://www.olx.ua/uk/list/{rc}/q-{slug}/"
    return f"https://www.olx.ua/uk/list/q-{slug}/"


def parse_olx_html(html_text: str, task: dict[str, Any], search_url: str) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    if not html_text or is_antibot_challenge(200, html_text):
        return [], _make_manual_link(
            task,
            channel="olx",
            route_country="UA",
            currency="UAH",
            reason_status="ANTIBOT_OR_AWS_WAF",
            direct_search_url=search_url,
        )

    dom = parse_dom(html_text)
    cards = dom.find_all(attrs={"data-cy": "l-card"})
    offers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    task_id = task.get("task_id", "")

    for idx, card in enumerate(cards):
        strings = card.stripped_strings()
        if not strings:
            continue
        a_tag = card.find("a", predicate=lambda n: bool(n.attr("href")))
        href = a_tag.attr("href").strip() if a_tag else ""
        if not href:
            continue
        full_url = f"https://www.olx.ua{href}" if href.startswith("/") else href
        if not is_direct_product_url(full_url, "olx"):
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        h_tag = card.find(("h4", "h6", "h3"))
        title = h_tag.get_text(" ", strip=True) if h_tag else strings[0]
        if len(title) < 3:
            continue

        img_tag = card.find("img")
        img_url = ""
        if img_tag:
            img_url = (img_tag.attr("src") or img_tag.attr("data-src")).strip()

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

        match_id = re.search(r"ID([a-zA-Z0-9]+)", href)
        offer_id = (
            f"olx_{match_id.group(1)}"
            if match_id
            else f"olx_{hashlib.sha1(full_url.encode('utf-8')).hexdigest()[:10]}_{idx}"
        )

        offers.append(
            {
                "offer_id": offer_id,
                "task_id": task_id,
                "channel": "olx",
                "route_country": "UA",
                "store_name": f"OLX ({location})" if location else "Продавець OLX",
                "trust_tier": 2,
                "title": title,
                "url": full_url,
                "image_url": img_url,
                "price_raw": price_val,
                "currency": "UAH",
                "shipping_raw": 0.0,
                "raw_price_snippet": raw_price_snippet,
                "raw_condition": condition_raw,
                "in_stock": True,
                "delivery_days": 2,
                "warranty_months": 0,
                "location": location,
                "specs": {"location": location, "escrow": "OLX Доставка"},
            }
        )

    if not offers:
        return [], _make_manual_link(
            task,
            channel="olx",
            route_country="UA",
            currency="UAH",
            reason_status="EMPTY_DOM_OR_AWS_WAF",
            direct_search_url=search_url,
        )
    return offers, None


def harvest_olx(
    task: dict[str, Any],
    query: str,
    *,
    use_chrome: bool = False,
    chrome_path: Optional[str] = None,
    timeout: float = 15.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    region_slugs = _infer_olx_region_slugs(task)
    all_offers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    manual_links: list[dict[str, Any]] = []

    for region_slug in region_slugs:
        url = build_olx_search_url(query, region_slug=region_slug)
        html_text = ""
        blocked_status: Optional[int] = None

        if use_chrome and chrome_path:
            html_text = _dump_dom_with_chrome(chrome_path, url, timeout=max(20.0, timeout))
        else:
            try:
                sc, body, final_url = http_get(url, referer="https://www.olx.ua/", timeout=timeout)
                if is_antibot_challenge(sc, body):
                    blocked_status = sc
                    # Optional clean Chrome fallback only to render JS DOM (never bypass CAPTCHA)
                    if chrome_path and use_chrome:
                        html_text = _dump_dom_with_chrome(chrome_path, url, timeout=max(20.0, timeout))
                    else:
                        html_text = ""
                else:
                    html_text = body
                    url = final_url or url
            except Exception as exc:
                manual_links.append(
                    _make_manual_link(
                        task,
                        channel="olx",
                        route_country="UA",
                        currency="UAH",
                        reason_status="HTTP_ERROR",
                        direct_search_url=url,
                        note=str(exc),
                    )
                )
                continue

        if blocked_status is not None and not html_text:
            manual_links.append(
                _make_manual_link(
                    task,
                    channel="olx",
                    route_country="UA",
                    currency="UAH",
                    reason_status=f"ANTIBOT_HTTP_{blocked_status}",
                    direct_search_url=url,
                )
            )
            continue

        offers, mlink = parse_olx_html(html_text, task, url)
        for off in offers:
            if off["url"] not in seen_urls:
                seen_urls.add(off["url"])
                all_offers.append(off)
        if mlink and not offers:
            manual_links.append(mlink)

    if all_offers:
        return all_offers, []
    return [], manual_links[:1]


# ---------------------------------------------------------------------------
# Rozetka 2-step API
# ---------------------------------------------------------------------------

def build_rozetka_search_url(query: str) -> str:
    return f"https://rozetka.com.ua/ua/search/?text={urllib.parse.quote(query)}&seller=rozetka"


def build_rozetka_api_url(query: str, category: str = "") -> str:
    base = (
        "https://search.rozetka.com.ua/ua/search/api/v6/"
        f"?front-type=xl&country=UA&lang=ua&text={urllib.parse.quote(query)}"
    )
    sec = ROZETKA_CATEGORY_SECTION_MAP.get((category or "").strip().lower())
    return f"{base}&section_id={sec}" if sec else base


def parse_rozetka_goods(
    goods: list[dict[str, Any]],
    task: dict[str, Any],
    manual_search_url: str,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    offers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    task_id = task.get("task_id", "")

    for item in goods:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or item.get("name") or "").strip()
        if not item_id or not title or item_id in seen_ids:
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

        seen_ids.add(item_id)
        price_pcs = str(item.get("price_pcs") or f"{int(price_val) if price_val.is_integer() else price_val} ₴")
        href = str(item.get("href") or f"https://rozetka.com.ua/ua/p{item_id}/").strip()
        if href.startswith("/"):
            href = f"https://rozetka.com.ua{href}"
        if not is_direct_product_url(href, "rozetka"):
            href = f"https://rozetka.com.ua/ua/p{item_id}/"

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
            store_name = "Rozetka.ua"
            trust_tier = 1
        elif seller_id is not None and not raw_seller:
            store_name = f"Rozetka Marketplace (#{seller_id})"
            trust_tier = 2
        else:
            store_name = str(raw_seller or item.get("brand") or "Rozetka Marketplace").strip()
            trust_tier = 2

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

        prod_obj = item.get("product") if isinstance(item.get("product"), dict) else {}
        state = str(prod_obj.get("state") or item.get("state") or "new").lower()
        if "refurb" in state:
            raw_condition = "Refurbished"
        elif "used" in state or "markdown" in state or item.get("is_markdown"):
            raw_condition = "Уцінка"
        else:
            raw_condition = "Нове"

        offers.append(
            {
                "offer_id": f"rozetka_{item_id}",
                "task_id": task_id,
                "channel": "rozetka",
                "route_country": "UA",
                "store_name": store_name,
                "trust_tier": trust_tier,
                "title": title,
                "url": href,
                "image_url": img_url,
                "price_raw": price_val,
                "currency": "UAH",
                "shipping_raw": 0.0,
                "raw_price_snippet": price_pcs,
                "raw_condition": raw_condition,
                "in_stock": in_stock,
                "delivery_days": 1 if is_official else 2,
                "warranty_months": 24 if is_official else 12,
                "seller_rating": seller_rating,
                "orders_or_reviews": reviews_count,
                "specs": {
                    "sell_status": sell_status,
                    "seller_id": seller_id,
                    "state": state,
                },
            }
        )

    if not offers:
        return [], _make_manual_link(
            task,
            channel="rozetka",
            route_country="UA",
            currency="UAH",
            reason_status="EMPTY_API_RESULT",
            direct_search_url=manual_search_url,
        )
    return offers, None


def harvest_rozetka(
    task: dict[str, Any],
    query: str,
    *,
    timeout: float = 15.0,
    max_hydrate: int = 10,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    api_url = build_rozetka_api_url(query, category=str(task.get("category") or ""))
    manual_url = build_rozetka_search_url(query)
    headers = {"Accept": "application/json, text/plain, */*"}

    try:
        sc, body, _ = http_get(api_url, headers=headers, referer="https://rozetka.com.ua/", timeout=timeout)
    except Exception as exc:
        return [], [
            _make_manual_link(
                task,
                channel="rozetka",
                route_country="UA",
                currency="UAH",
                reason_status="HTTP_ERROR",
                direct_search_url=manual_url,
                note=str(exc),
            )
        ]

    if is_antibot_challenge(sc, body):
        return [], [
            _make_manual_link(
                task,
                channel="rozetka",
                route_country="UA",
                currency="UAH",
                reason_status=f"ANTIBOT_HTTP_{sc}",
                direct_search_url=manual_url,
            )
        ]

    try:
        payload = json.loads(body)
    except Exception as exc:
        return [], [
            _make_manual_link(
                task,
                channel="rozetka",
                route_country="UA",
                currency="UAH",
                reason_status="NON_JSON_OR_WAF",
                direct_search_url=manual_url,
                note=str(exc),
            )
        ]

    data = payload.get("data") if isinstance(payload, dict) else None
    raw_goods = (data or {}).get("goods") if isinstance(data, dict) else None
    if not isinstance(raw_goods, list) or not raw_goods:
        return [], [
            _make_manual_link(
                task,
                channel="rozetka",
                route_country="UA",
                currency="UAH",
                reason_status="EMPTY_API_RESULT",
                direct_search_url=manual_url,
            )
        ]

    hydrated: list[dict[str, Any]] = []
    stub_ids: list[str] = []
    for g in raw_goods:
        if not isinstance(g, dict):
            continue
        if (g.get("title") or g.get("name")) and g.get("price") is not None:
            hydrated.append(g)
        elif g.get("id") is not None and len(stub_ids) < max_hydrate:
            stub_ids.append(str(g["id"]))

    if stub_ids:
        def _fetch_stub(gid: str) -> Optional[dict[str, Any]]:
            p_url = (
                "https://product-api.rozetka.com.ua/v4/goods/get-main"
                f"?front-type=xl&country=UA&lang=ua&goodsId={gid}"
            )
            try:
                psc, pbody, _ = http_get(p_url, headers=headers, referer="https://rozetka.com.ua/", timeout=timeout)
                if psc != 200 or is_antibot_challenge(psc, pbody):
                    return None
                parsed = json.loads(pbody)
                item_data = parsed.get("data") if isinstance(parsed, dict) else None
                if isinstance(item_data, dict):
                    item_data.setdefault("id", gid)
                    return item_data
            except Exception:
                return None
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(stub_ids))) as pool:
            for res in pool.map(_fetch_stub, stub_ids):
                if res is not None:
                    hydrated.append(res)

    offers, mlink = parse_rozetka_goods(hydrated, task, manual_url)
    return offers, ([mlink] if mlink else [])


# ---------------------------------------------------------------------------
# Prom.ua
# ---------------------------------------------------------------------------

def build_prom_search_url(query: str) -> str:
    return f"https://prom.ua/ua/search?search_term={urllib.parse.quote_plus(query)}"


def parse_prom_html(html_text: str, task: dict[str, Any], search_url: str) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    if not html_text or is_antibot_challenge(200, html_text):
        return [], _make_manual_link(
            task,
            channel="prom",
            route_country="UA",
            currency="UAH",
            reason_status="ANTIBOT_OR_WAF_BLOCKED",
            direct_search_url=search_url,
        )

    dom = parse_dom(html_text)
    cards = dom.find_all(attrs={"data-qaid": "product_block"})
    offers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    task_id = task.get("task_id", "")

    for idx, card in enumerate(cards):
        a_link = card.find(attrs={"data-qaid": "product_link"}) or card.find("a", predicate=lambda n: bool(n.attr("href")))
        if not a_link:
            continue
        href = a_link.attr("href").strip()
        if not href:
            continue
        full_url = f"https://prom.ua{href}" if href.startswith("/") else href
        if not is_direct_product_url(full_url, "prom"):
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        title_el = card.find(attrs={"data-qaid": "product_name"}) or a_link
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if len(title) < 4:
            continue

        price_el = card.find(attrs={"data-qaid": "product_price"})
        raw_snippet = price_el.get_text(" ", strip=True) if price_el else ""
        price_val = parse_price_value(raw_snippet)
        if price_val is None or price_val <= 20:
            continue

        img_tag = card.find("img")
        img_url = (img_tag.attr("src") or img_tag.attr("data-src")).strip() if img_tag else ""

        comp_el = card.find(attrs={"data-qaid": "company_name"})
        company = comp_el.get_text(" ", strip=True) if comp_el else "Магазин Prom"

        pres_el = card.find(attrs={"data-qaid": "product_presence"})
        pres_txt = pres_el.get_text(" ", strip=True) if pres_el else "В наявності"
        in_stock = "немає" not in pres_txt.lower() and "відсутн" not in pres_txt.lower()

        t_low = title.lower()
        if any(w in t_low for w in ("б/у", "б/в", "вживан")):
            raw_cond = "Вживане"
        elif any(w in t_low for w in ("refurbished", "відновлен", "після сервісу")):
            raw_cond = "Відновлений"
        elif any(w in t_low for w in ("open box", "вітринн", "розпакован", "уцінка")):
            raw_cond = "Open Box"
        else:
            raw_cond = "Нове"

        match_id = re.search(r"p(\d+)", href) or re.search(r"m(\d+)", href)
        offer_id = (
            f"prom_{match_id.group(1)}"
            if match_id
            else f"prom_{hashlib.sha1(full_url.encode('utf-8')).hexdigest()[:10]}_{idx}"
        )

        offers.append(
            {
                "offer_id": offer_id,
                "task_id": task_id,
                "channel": "prom",
                "route_country": "UA",
                "store_name": company,
                "trust_tier": 2,
                "title": title,
                "url": full_url,
                "image_url": img_url,
                "price_raw": price_val,
                "currency": "UAH",
                "shipping_raw": 0.0,
                "raw_price_snippet": raw_snippet,
                "raw_condition": raw_cond,
                "in_stock": in_stock,
                "delivery_days": 2,
                "warranty_months": 12,
                "specs": {"presence": pres_txt, "escrow": "Prom-оплата"},
            }
        )

    if not offers:
        return [], _make_manual_link(
            task,
            channel="prom",
            route_country="UA",
            currency="UAH",
            reason_status="EMPTY_DOM_OR_WAF",
            direct_search_url=search_url,
        )
    return offers, None


def harvest_prom(
    task: dict[str, Any],
    query: str,
    *,
    timeout: float = 15.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    url = build_prom_search_url(query)
    try:
        sc, body, final_url = http_get(url, referer="https://prom.ua/", timeout=timeout)
    except Exception as exc:
        return [], [
            _make_manual_link(
                task,
                channel="prom",
                route_country="UA",
                currency="UAH",
                reason_status="HTTP_ERROR",
                direct_search_url=url,
                note=str(exc),
            )
        ]

    if is_antibot_challenge(sc, body):
        return [], [
            _make_manual_link(
                task,
                channel="prom",
                route_country="UA",
                currency="UAH",
                reason_status=f"ANTIBOT_HTTP_{sc}",
                direct_search_url=final_url or url,
            )
        ]

    offers, mlink = parse_prom_html(body, task, url)
    return offers, ([mlink] if mlink else [])


# ---------------------------------------------------------------------------
# Hotline.ua (JSON-LD + Search HTML)
# ---------------------------------------------------------------------------

def build_hotline_search_url(query: str) -> str:
    return f"https://hotline.ua/ua/sr/?q={urllib.parse.quote(query)}"


def _extract_jsonld_objects(dom: MiniNode) -> list[dict[str, Any]]:
    objs: list[dict[str, Any]] = []
    for script in dom.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if isinstance(parsed, dict):
            objs.append(parsed)
        elif isinstance(parsed, list):
            objs.extend(item for item in parsed if isinstance(item, dict))
    return objs


def parse_hotline_html(html_text: str, task: dict[str, Any], search_url: str) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    if not html_text or is_antibot_challenge(200, html_text):
        return [], _make_manual_link(
            task,
            channel="hotline",
            route_country="UA",
            currency="UAH",
            reason_status="ANTIBOT_OR_WAF_BLOCKED",
            direct_search_url=search_url,
        )

    dom = parse_dom(html_text)
    offers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    task_id = task.get("task_id", "")

    # 1. JSON-LD Product / ItemList extraction
    for obj in _extract_jsonld_objects(dom):
        otype = str(obj.get("@type") or "").lower()
        items_to_check: list[dict[str, Any]] = []
        if otype == "product":
            items_to_check.append(obj)
        elif otype == "itemlist" and isinstance(obj.get("itemListElement"), list):
            for el in obj["itemListElement"]:
                if isinstance(el, dict):
                    item = el.get("item") if isinstance(el.get("item"), dict) else el
                    if isinstance(item, dict):
                        items_to_check.append(item)

        for idx, prod in enumerate(items_to_check):
            title = str(prod.get("name") or "").strip()
            url = str(prod.get("url") or search_url).strip()
            if url.startswith("/"):
                url = f"https://hotline.ua{url}"
            if not title or not is_direct_product_url(url, "hotline") or url in seen_urls:
                continue

            offers_blk = prod.get("offers")
            if isinstance(offers_blk, list) and offers_blk:
                offers_blk = offers_blk[0]
            if not isinstance(offers_blk, dict):
                continue

            low_raw = offers_blk.get("lowPrice") or offers_blk.get("price")
            price_val = parse_price_value(str(low_raw)) if low_raw is not None else None
            if price_val is None or price_val <= 0:
                continue

            seen_urls.add(url)
            offer_count = 1
            try:
                offer_count = int(offers_blk.get("offerCount") or 1)
            except (TypeError, ValueError):
                offer_count = 1
            cur = str(offers_blk.get("priceCurrency") or "UAH").upper()
            snippet = f"{int(price_val) if price_val.is_integer() else price_val} ₴"
            offer_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
            offers.append(
                {
                    "offer_id": f"hotline_ld_{offer_hash}_{idx}",
                    "task_id": task_id,
                    "channel": "hotline",
                    "route_country": "UA",
                    "store_name": "Hotline Catalog (Official UA)",
                    "trust_tier": 1,
                    "title": title,
                    "url": url,
                    "image_url": str(prod.get("image") or "") if isinstance(prod.get("image"), str) else "",
                    "price_raw": price_val,
                    "currency": cur,
                    "shipping_raw": 0.0,
                    "raw_price_snippet": snippet,
                    "raw_condition": "Нове",
                    "in_stock": True,
                    "delivery_days": 1,
                    "warranty_months": 24,
                    "orders_or_reviews": offer_count,
                    "specs": {"offers_count": offer_count, "source": "json_ld"},
                }
            )

    # 2. Search HTML cards
    cards = dom.find_all(
        tags=("div", "li", "article"),
        predicate=lambda n: (
            n.has_class("list-item")
            or n.has_class("product-item")
            or n.has_class("item-info")
            or n.has_class("product-card")
            or bool(n.attr("data-product-id"))
        ),
    )
    if not cards:
        cards = dom.find_all(
            tags=("div", "li", "article"),
            predicate=lambda n: (
                ("грн" in n.get_text(" ", strip=True) or "₴" in n.get_text(" ", strip=True))
                and bool(n.find("a", predicate=lambda a: bool(a.attr("href"))))
                and 15 < len(n.get_text(" ", strip=True)) < 600
            ),
        )

    for idx, card in enumerate(cards):
        title_el = card.find(
            "a",
            predicate=lambda a: a.has_class("item-title") or bool(a.attr("href")),
        )
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        href = title_el.attr("href").strip()
        if not title or len(title) < 4 or not href:
            continue
        full_url = f"https://hotline.ua{href}" if href.startswith("/") else href
        if not is_direct_product_url(full_url, "hotline") or full_url in seen_urls:
            continue

        price_el = card.find(
            tags=("span", "div", "p", "strong", "b"),
            predicate=lambda n: (
                any(
                    c in n.attrs.get("class", "")
                    for c in ("list-item__value-price", "price-md", "price__value", "product-item__price", "price")
                )
                or n.attr("data-qaid") == "product_price"
            ),
        )
        raw_snippet = price_el.get_text(" ", strip=True) if price_el else ""
        if not raw_snippet:
            for node in card.find_all(("span", "div", "p", "strong", "b")):
                nt = node.get_text(" ", strip=True)
                if ("грн" in nt.lower() or "₴" in nt) and re.search(r"\d", nt) and len(nt) <= 60:
                    raw_snippet = nt
                    break

        min_price, max_price = parse_price_range(raw_snippet)
        if min_price is None or min_price <= 0:
            continue

        seen_urls.add(full_url)
        img_tag = card.find("img")
        img_url = (img_tag.attr("src") or img_tag.attr("data-src")).strip() if img_tag else ""
        if img_url.startswith("//"):
            img_url = f"https:{img_url}"

        offers_cnt = 1
        cnt_el = card.find(predicate=lambda n: "offers-count" in n.attrs.get("class", ""))
        if cnt_el:
            m_cnt = re.search(r"(\d+)", cnt_el.get_text(" ", strip=True))
            if m_cnt:
                offers_cnt = int(m_cnt.group(1))

        specs: dict[str, Any] = {"offers_count": offers_cnt}
        if max_price is not None and max_price > min_price:
            specs["price_range_max_uah"] = max_price

        offer_hash = hashlib.sha1(full_url.encode("utf-8")).hexdigest()[:10]
        offers.append(
            {
                "offer_id": f"hotline_{offer_hash}_{idx}",
                "task_id": task_id,
                "channel": "hotline",
                "route_country": "UA",
                "store_name": "Hotline Catalog (Official UA)",
                "trust_tier": 1,
                "title": title,
                "url": full_url,
                "image_url": img_url,
                "price_raw": min_price,
                "currency": "UAH",
                "shipping_raw": 0.0,
                "raw_price_snippet": raw_snippet,
                "raw_condition": "Нове",
                "in_stock": True,
                "delivery_days": 1,
                "warranty_months": 24,
                "orders_or_reviews": offers_cnt,
                "specs": specs,
            }
        )

    if not offers:
        return [], _make_manual_link(
            task,
            channel="hotline",
            route_country="UA",
            currency="UAH",
            reason_status="EMPTY_DOM_OR_WAF",
            direct_search_url=search_url,
        )
    return offers, None


def harvest_hotline(
    task: dict[str, Any],
    query: str,
    *,
    timeout: float = 15.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    url = build_hotline_search_url(query)
    try:
        sc, body, final_url = http_get(url, referer="https://hotline.ua/", timeout=timeout)
    except Exception as exc:
        return [], [
            _make_manual_link(
                task,
                channel="hotline",
                route_country="UA",
                currency="UAH",
                reason_status="HTTP_ERROR",
                direct_search_url=url,
                note=str(exc),
            )
        ]

    if is_antibot_challenge(sc, body):
        return [], [
            _make_manual_link(
                task,
                channel="hotline",
                route_country="UA",
                currency="UAH",
                reason_status=f"ANTIBOT_HTTP_{sc}",
                direct_search_url=final_url or url,
            )
        ]

    offers, mlink = parse_hotline_html(body, task, final_url or url)
    return offers, ([mlink] if mlink else [])


# ---------------------------------------------------------------------------
# AliExpress (Embedded JSON + HTML card fallback + x5sec fail-closed)
# ---------------------------------------------------------------------------

def build_aliexpress_search_url(query: str) -> str:
    return f"https://www.aliexpress.com/wholesale?SearchText={urllib.parse.quote_plus(query)}"


def _extract_ali_init_json(html_text: str) -> Optional[dict[str, Any]]:
    markers = ("_init_data_=", "_init_data_ =", "window._d_c_.DCData=", "window.runParams=")
    decoder = json.JSONDecoder()
    for marker in markers:
        pos = 0
        while True:
            idx = html_text.find(marker, pos)
            if idx == -1:
                break
            pos = idx + len(marker)
            start = html_text.find("{", pos)
            if start == -1:
                continue
            try:
                obj, _ = decoder.raw_decode(html_text, start)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    return None


def _find_ali_item_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        if "itemList" in data and isinstance(data["itemList"], dict):
            content = data["itemList"].get("content")
            if isinstance(content, list):
                return [item for item in content if isinstance(item, dict)]
        if "mods" in data and isinstance(data["mods"], dict):
            il = data["mods"].get("itemList")
            if isinstance(il, dict) and isinstance(il.get("content"), list):
                return [item for item in il["content"] if isinstance(item, dict)]
        for v in data.values():
            found = _find_ali_item_list(v)
            if found:
                return found
    elif isinstance(data, list):
        for entry in data:
            found = _find_ali_item_list(entry)
            if found:
                return found
    return []


def parse_aliexpress_html(
    html_text: str,
    task: dict[str, Any],
    search_url: str,
    *,
    route_country: str = "UA",
    currency: str = "USD",
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    lower = (html_text or "").lower()
    if (
        not html_text
        or "punish" in lower
        or "x5sec" in lower
        or "_____tmd_____" in lower
        or is_antibot_challenge(200, html_text)
    ):
        return [], _make_manual_link(
            task,
            channel="aliexpress",
            route_country=route_country,
            currency=currency,
            reason_status="ANTIBOT_CHALLENGE_PUNISH",
            direct_search_url=search_url,
        )

    offers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    task_id = task.get("task_id", "")

    payload = _extract_ali_init_json(html_text)
    if payload:
        for item in _find_ali_item_list(payload):
            pid = str(item.get("productId") or item.get("itemId") or "").strip()
            if not pid or pid in seen_ids:
                continue
            title_obj = item.get("title") or {}
            title = (
                str(title_obj.get("displayTitle") or title_obj.get("seoTitle") or "").strip()
                if isinstance(title_obj, dict)
                else str(title_obj).strip()
            )
            if not title:
                continue
            prices = item.get("prices") or {}
            sale_obj = prices.get("salePrice") or {}
            raw_p = sale_obj.get("minPrice") or sale_obj.get("formattedPrice") or item.get("salePrice")
            price_val = parse_price_value(str(raw_p)) if raw_p is not None else None
            if price_val is None or price_val <= 0:
                continue

            seen_ids.add(pid)
            item_cur = str(sale_obj.get("currencyCode") or currency).upper()
            snippet = str(sale_obj.get("formattedPrice") or f"{price_val:.2f} {item_cur}")
            store_obj = item.get("store") or {}
            store_name = str(store_obj.get("storeName") or "AliExpress Store")
            is_official = any(k in store_name.lower() for k in ("official", "flagship", "choice", "brand"))

            selling_points = item.get("sellingPoints") or []
            is_choice = False
            free_shipping = False
            for sp in selling_points:
                if isinstance(sp, dict):
                    tt = str(sp.get("tagContent", {}).get("tagText") or sp.get("text") or "").lower()
                    if "choice" in tt:
                        is_choice = True
                    if "free shipping" in tt or "free delivery" in tt:
                        free_shipping = True

            if is_choice and "choice" not in store_name.lower():
                store_name = f"{store_name} (Choice)"

            eval_obj = item.get("evaluation") or {}
            rating = parse_price_value(str(eval_obj.get("starRating") or item.get("starRating") or ""))
            if rating is not None and rating > 5.0:
                rating = 5.0

            trade_obj = item.get("trade") or {}
            orders = 0
            m_ord = re.search(r"([\d,]+)", str(trade_obj.get("tradeDesc") or item.get("orders") or ""))
            if m_ord:
                orders = int(m_ord.group(1).replace(",", ""))

            img_obj = item.get("image") or {}
            img_url = str(img_obj.get("imgUrl") or "")
            if img_url.startswith("//"):
                img_url = f"https:{img_url}"

            offers.append(
                {
                    "offer_id": f"ali_{route_country.lower()}_{pid}",
                    "task_id": task_id,
                    "channel": "aliexpress",
                    "route_country": route_country,
                    "store_name": store_name,
                    "trust_tier": 1 if (is_official or is_choice) else 2,
                    "title": title,
                    "url": f"https://www.aliexpress.com/item/{pid}.html",
                    "image_url": img_url,
                    "price_raw": price_val,
                    "currency": item_cur,
                    "shipping_raw": 0.0,
                    "raw_price_snippet": snippet,
                    "raw_condition": "New",
                    "in_stock": True,
                    "delivery_days": 14 if route_country == "UA" else 18,
                    "warranty_months": 12,
                    "seller_rating": rating,
                    "orders_or_reviews": orders,
                    "specs": {"free_shipping": free_shipping, "is_choice": is_choice},
                }
            )

    # Fallback HTML /item/<id>.html anchors if JSON missing
    if not offers:
        dom = parse_dom(html_text)
        for a in dom.find_all("a", predicate=lambda n: "/item/" in n.attr("href")):
            href = a.attr("href").strip()
            m_pid = re.search(r"/item/(\d+)\.html", href)
            if not m_pid:
                continue
            pid = m_pid.group(1)
            if pid in seen_ids:
                continue
            raw_card_text = a.get_text(" ", strip=True)
            if len(raw_card_text) < 6:
                continue
            m_price_explicit = re.search(
                r"(?:US\s*\$|\$|€|EUR|USD)\s*(\d+(?:[.,]\d{1,2})?)",
                raw_card_text,
                re.IGNORECASE,
            )
            if m_price_explicit:
                try:
                    shown_price = float(m_price_explicit.group(1).replace(",", "."))
                except ValueError:
                    shown_price = None
            else:
                shown_price = parse_price_value(raw_card_text)
            if shown_price is None or shown_price <= 0:
                continue

            specs: dict[str, Any] = {"free_shipping": "free shipping" in raw_card_text.lower()}
            m_welcome = re.search(
                r"New\s+shoppers\s*(?:save|-)\s*(?:US\s*\$|\$)\s*(\d+(?:[.,]\d{1,2})?)",
                raw_card_text,
                re.IGNORECASE,
            )
            if m_welcome:
                welcome_disc = float(m_welcome.group(1).replace(",", "."))
                est_existing = round(shown_price * 1.08, 2)
                price_val = shown_price
                raw_price_snippet = (
                    f"US ${shown_price:.2f} (Welcome Deal; ~${est_existing:.2f} існуючий акаунт до купонів)"
                )
                specs["welcome_price_usd"] = shown_price
                specs["welcome_discount_usd"] = welcome_disc
                specs["est_existing_price_usd"] = est_existing
                specs["is_welcome_deal_detected"] = True
            else:
                price_val = shown_price
                raw_price_snippet = f"US ${price_val:.2f}"

            # Clean title before price / UI chrome ("US $...", "See preview", "Similar items")
            clean_title = re.split(
                r"\s+(?:US\s*\$|\$|€|See preview|Similar items|\d[\d,]*\+?\s*sold)\b",
                raw_card_text,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()
            if len(clean_title) < 6:
                clean_title = raw_card_text[:120].strip()

            m_sold = re.search(r"(\d[\d,]*)\+?\s*sold", raw_card_text, re.IGNORECASE)
            orders_cnt = int(m_sold.group(1).replace(",", "")) if m_sold else 0
            m_rating = re.search(r"(?:^|\s)([45](?:\.\d)?)\s+\d[\d,]*\+?\s*sold", raw_card_text, re.IGNORECASE)
            rating_val = float(m_rating.group(1)) if m_rating else None

            seen_ids.add(pid)
            offers.append(
                {
                    "offer_id": f"ali_{route_country.lower()}_{pid}",
                    "task_id": task_id,
                    "channel": "aliexpress",
                    "route_country": route_country,
                    "store_name": "AliExpress Store",
                    "trust_tier": 2,
                    "title": clean_title,
                    "url": f"https://www.aliexpress.com/item/{pid}.html",
                    "image_url": "",
                    "price_raw": price_val,
                    "currency": currency,
                    "shipping_raw": 0.0,
                    "raw_price_snippet": raw_price_snippet,
                    "raw_condition": "New",
                    "in_stock": True,
                    "delivery_days": 14,
                    "warranty_months": 12,
                    "seller_rating": rating_val,
                    "orders_or_reviews": orders_cnt,
                    "specs": specs,
                }
            )

    if not offers:
        return [], _make_manual_link(
            task,
            channel="aliexpress",
            route_country=route_country,
            currency=currency,
            reason_status="PARSE_JSON_MISSING",
            direct_search_url=search_url,
        )
    return offers, None


def harvest_aliexpress(
    task: dict[str, Any],
    query: str,
    *,
    timeout: float = 20.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    url = build_aliexpress_search_url(query)
    cookies = {"aep_usuc_f": "site=glo&c_tp=USD&region=UA&b_locale=en_US"}
    try:
        sc, body, final_url = http_get(
            url,
            cookies=cookies,
            accept_language="en-US,en;q=0.9",
            timeout=timeout,
        )
    except Exception as exc:
        return [], [
            _make_manual_link(
                task,
                channel="aliexpress",
                route_country="UA",
                currency="USD",
                reason_status="HTTP_ERROR",
                direct_search_url=url,
                note=str(exc),
            )
        ]

    if is_antibot_challenge(sc, body):
        return [], [
            _make_manual_link(
                task,
                channel="aliexpress",
                route_country="UA",
                currency="USD",
                reason_status=f"ANTIBOT_HTTP_{sc}",
                direct_search_url=final_url or url,
            )
        ]

    offers, mlink = parse_aliexpress_html(body, task, url)
    return offers, ([mlink] if mlink else [])


# ---------------------------------------------------------------------------
# Europe: Idealo.de, Ceneo.pl, Amazon.de
# ---------------------------------------------------------------------------

def build_idealo_search_url(query: str) -> str:
    return f"https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={urllib.parse.quote_plus(query)}"


def parse_idealo_html(html_text: str, task: dict[str, Any], search_url: str) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    if not html_text or is_antibot_challenge(200, html_text):
        return [], _make_manual_link(
            task,
            channel="idealo_de",
            route_country="DE",
            currency="EUR",
            reason_status="ANTIBOT_CLOUDFLARE_OR_DATADOME",
            direct_search_url=search_url,
        )

    dom = parse_dom(html_text)
    cards = dom.find_all(
        tags=("div", "article", "li"),
        predicate=lambda n: (
            "sr-resultItemTile" in n.attrs.get("class", "")
            or "sr-resultList__item" in n.attrs.get("class", "")
            or "sr-resultItem" in n.attrs.get("class", "")
            or n.attr("data-testid") == "resultItem"
        ),
    )
    if not cards:
        cards = dom.find_all(
            tags=("div", "article", "li"),
            predicate=lambda n: (
                "€" in n.get_text(" ", strip=True)
                and bool(n.find("a", predicate=lambda a: bool(a.attr("href"))))
                and 15 < len(n.get_text(" ", strip=True)) < 500
            ),
        )

    offers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    task_id = task.get("task_id", "")

    for idx, card in enumerate(cards):
        a_el = card.find(
            "a",
            predicate=lambda a: (
                "sr-resultItemLink" in a.attrs.get("class", "")
                or "sr-resultItemTile__link" in a.attrs.get("class", "")
                or bool(a.attr("href"))
            ),
        )
        if not a_el:
            continue
        href = a_el.attr("href").strip()
        title_el = card.find(predicate=lambda n: n.attr("data-testid") == "product-title") or a_el
        title = title_el.get_text(" ", strip=True)
        if not href or len(title) < 4:
            continue
        full_url = f"https://www.idealo.de{href}" if href.startswith("/") else href
        if not is_direct_product_url(full_url, "idealo_de") or full_url in seen_urls:
            continue

        raw_snippet = ""
        price_el = card.find(
            predicate=lambda n: (
                n.attr("data-testid") == "price"
                or "sr-detailedPriceInfo__price" in n.attrs.get("class", "")
                or "sr-resultItemTile__price" in n.attrs.get("class", "")
            )
        )
        if price_el:
            raw_snippet = price_el.get_text(" ", strip=True)
        if not raw_snippet:
            for node in card.find_all(("span", "div", "strong", "a")):
                nt = node.get_text(" ", strip=True)
                if "€" in nt and re.search(r"\d", nt) and len(nt) <= 45:
                    raw_snippet = nt
                    break

        price_val = parse_price_value(raw_snippet)
        if price_val is None or price_val <= 0:
            continue

        seen_urls.add(full_url)
        offer_hash = hashlib.sha1(full_url.encode("utf-8")).hexdigest()[:10]
        offers.append(
            {
                "offer_id": f"idealo_de_{offer_hash}_{idx}",
                "task_id": task_id,
                "channel": "idealo_de",
                "route_country": "DE",
                "store_name": "Idealo.de Catalog",
                "trust_tier": 1,
                "title": title,
                "url": full_url,
                "image_url": "",
                "price_raw": price_val,
                "currency": "EUR",
                "shipping_raw": 0.0,
                "raw_price_snippet": raw_snippet,
                "raw_condition": "Neu",
                "in_stock": True,
                "delivery_days": 8,
                "warranty_months": 24,
                "specs": {},
            }
        )

    if not offers:
        return [], _make_manual_link(
            task,
            channel="idealo_de",
            route_country="DE",
            currency="EUR",
            reason_status="EMPTY_DOM_OR_ANTIBOT",
            direct_search_url=search_url,
        )
    return offers, None


def harvest_idealo_de(task: dict[str, Any], query: str, *, timeout: float = 15.0) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    url = build_idealo_search_url(query)
    try:
        sc, body, final_url = http_get(
            url,
            referer="https://www.idealo.de/",
            accept_language="de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            timeout=timeout,
        )
    except Exception as exc:
        return [], [
            _make_manual_link(
                task,
                channel="idealo_de",
                route_country="DE",
                currency="EUR",
                reason_status="HTTP_ERROR",
                direct_search_url=url,
                note=str(exc),
            )
        ]

    if is_antibot_challenge(sc, body):
        return [], [
            _make_manual_link(
                task,
                channel="idealo_de",
                route_country="DE",
                currency="EUR",
                reason_status=f"ANTIBOT_HTTP_{sc}",
                direct_search_url=final_url or url,
            )
        ]

    offers, mlink = parse_idealo_html(body, task, url)
    return offers, ([mlink] if mlink else [])


def build_ceneo_search_url(query: str) -> str:
    return f"https://www.ceneo.pl/;szukaj-{urllib.parse.quote_plus(query)}"


def parse_ceneo_html(html_text: str, task: dict[str, Any], search_url: str) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    if not html_text or is_antibot_challenge(200, html_text):
        return [], _make_manual_link(
            task,
            channel="ceneo_pl",
            route_country="PL",
            currency="PLN",
            reason_status="ANTIBOT_CLOUDFLARE_OR_DATADOME",
            direct_search_url=search_url,
        )

    dom = parse_dom(html_text)
    cards = dom.find_all(
        tags=("div", "article"),
        predicate=lambda n: (
            n.has_class("cat-prod-row")
            or n.has_class("js_category-list-item")
            or n.has_class("cat-prod-box")
            or bool(n.attr("data-pid"))
        ),
    )
    offers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    task_id = task.get("task_id", "")

    for idx, card in enumerate(cards):
        a_el = card.find(
            "a",
            predicate=lambda a: a.has_class("go-to-product") or bool(a.attr("href")),
        )
        if not a_el:
            continue
        href = a_el.attr("href").strip()
        title = a_el.get_text(" ", strip=True)
        if not href or len(title) < 4:
            continue
        full_url = f"https://www.ceneo.pl{href}" if href.startswith("/") else href
        if not is_direct_product_url(full_url, "ceneo_pl") or full_url in seen_urls:
            continue

        raw_snippet = ""
        for node in card.find_all(("span", "div", "strong", "a")):
            nt = node.get_text(" ", strip=True)
            if ("zł" in nt.lower() or "pln" in nt.lower()) and re.search(r"\d", nt) and len(nt) <= 45:
                raw_snippet = nt
                break
        price_val = parse_price_value(raw_snippet)
        if price_val is None or price_val <= 0:
            continue

        seen_urls.add(full_url)
        pid = card.attr("data-pid").strip()
        offer_id = f"ceneo_pl_{pid}" if pid else f"ceneo_pl_{hashlib.sha1(full_url.encode('utf-8')).hexdigest()[:10]}_{idx}"
        offers.append(
            {
                "offer_id": offer_id,
                "task_id": task_id,
                "channel": "ceneo_pl",
                "route_country": "PL",
                "store_name": "Ceneo.pl Catalog",
                "trust_tier": 1,
                "title": title,
                "url": full_url,
                "image_url": "",
                "price_raw": price_val,
                "currency": "PLN",
                "shipping_raw": 0.0,
                "raw_price_snippet": raw_snippet,
                "raw_condition": "Nowy",
                "in_stock": True,
                "delivery_days": 6,
                "warranty_months": 24,
                "specs": {},
            }
        )

    if not offers:
        return [], _make_manual_link(
            task,
            channel="ceneo_pl",
            route_country="PL",
            currency="PLN",
            reason_status="EMPTY_DOM_OR_ANTIBOT",
            direct_search_url=search_url,
        )
    return offers, None


def harvest_ceneo_pl(task: dict[str, Any], query: str, *, timeout: float = 15.0) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    url = build_ceneo_search_url(query)
    try:
        sc, body, final_url = http_get(
            url,
            referer="https://www.ceneo.pl/",
            accept_language="pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
            timeout=timeout,
        )
    except Exception as exc:
        return [], [
            _make_manual_link(
                task,
                channel="ceneo_pl",
                route_country="PL",
                currency="PLN",
                reason_status="HTTP_ERROR",
                direct_search_url=url,
                note=str(exc),
            )
        ]

    if is_antibot_challenge(sc, body):
        return [], [
            _make_manual_link(
                task,
                channel="ceneo_pl",
                route_country="PL",
                currency="PLN",
                reason_status=f"ANTIBOT_HTTP_{sc}",
                direct_search_url=final_url or url,
            )
        ]

    offers, mlink = parse_ceneo_html(body, task, url)
    return offers, ([mlink] if mlink else [])


def build_amazon_de_search_url(query: str) -> str:
    return f"https://www.amazon.de/s?k={urllib.parse.quote_plus(query)}"


def parse_amazon_de_html(html_text: str, task: dict[str, Any], search_url: str) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    if not html_text or is_antibot_challenge(200, html_text):
        return [], _make_manual_link(
            task,
            channel="amazon_de",
            route_country="DE",
            currency="EUR",
            reason_status="ANTIBOT_AMAZON_CAPTCHA",
            direct_search_url=search_url,
        )

    dom = parse_dom(html_text)
    cards = dom.find_all("div", attrs={"data-component-type": "s-search-result"})
    offers: list[dict[str, Any]] = []
    seen_asins: set[str] = set()
    task_id = task.get("task_id", "")

    for card in cards:
        asin = card.attr("data-asin").strip()
        if not asin or asin in seen_asins:
            continue
        h2_el = card.find("h2")
        title = h2_el.get_text(" ", strip=True) if h2_el else ""
        if len(title) < 4:
            continue
        link_el = (h2_el.find("a", predicate=lambda a: bool(a.attr("href"))) if h2_el else None) or card.find(
            "a", predicate=lambda a: "/dp/" in a.attr("href")
        )
        href = link_el.attr("href").strip() if link_el else f"/dp/{asin}"
        full_url = f"https://www.amazon.de{href}" if href.startswith("/") else href
        if not is_direct_product_url(full_url, "amazon_de"):
            full_url = f"https://www.amazon.de/dp/{asin}"

        raw_snippet = ""
        offscreen = card.find("span", predicate=lambda n: n.has_class("a-offscreen"))
        if offscreen:
            raw_snippet = offscreen.get_text(" ", strip=True)
        price_val = parse_price_value(raw_snippet)
        if price_val is None or price_val <= 0:
            whole = card.find(predicate=lambda n: n.has_class("a-price-whole"))
            frac = card.find(predicate=lambda n: n.has_class("a-price-fraction"))
            if whole:
                w = re.sub(r"[^\d]", "", whole.get_text("", strip=True))
                f = re.sub(r"[^\d]", "", frac.get_text("", strip=True)) if frac else "00"
                if w:
                    price_val = float(f"{w}.{f or '00'}")
                    raw_snippet = f"{price_val:.2f} €"

        if price_val is None or price_val <= 0:
            continue

        seen_asins.add(asin)
        offers.append(
            {
                "offer_id": f"amz_de_{asin}",
                "task_id": task_id,
                "channel": "amazon_de",
                "route_country": "DE",
                "store_name": "Amazon DE",
                "trust_tier": 1,
                "title": title,
                "url": full_url,
                "image_url": "",
                "price_raw": price_val,
                "currency": "EUR",
                "shipping_raw": 25.0,
                "raw_price_snippet": raw_snippet,
                "raw_condition": "New",
                "in_stock": True,
                "delivery_days": 8,
                "warranty_months": 24,
                "specs": {"asin": asin},
            }
        )

    if not offers:
        return [], _make_manual_link(
            task,
            channel="amazon_de",
            route_country="DE",
            currency="EUR",
            reason_status="EMPTY_DOM_OR_CAPTCHA",
            direct_search_url=search_url,
        )
    return offers, None


def harvest_amazon_de(task: dict[str, Any], query: str, *, timeout: float = 15.0) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    url = build_amazon_de_search_url(query)
    try:
        sc, body, final_url = http_get(
            url,
            referer="https://www.amazon.de/",
            accept_language="de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            timeout=timeout,
        )
    except Exception as exc:
        return [], [
            _make_manual_link(
                task,
                channel="amazon_de",
                route_country="DE",
                currency="EUR",
                reason_status="HTTP_ERROR",
                direct_search_url=url,
                note=str(exc),
            )
        ]

    if is_antibot_challenge(sc, body):
        return [], [
            _make_manual_link(
                task,
                channel="amazon_de",
                route_country="DE",
                currency="EUR",
                reason_status=f"ANTIBOT_HTTP_{sc}",
                direct_search_url=final_url or url,
            )
        ]

    offers, mlink = parse_amazon_de_html(body, task, url)
    return offers, ([mlink] if mlink else [])


# ---------------------------------------------------------------------------
# Task Execution & Output Rendering
# ---------------------------------------------------------------------------

CHANNEL_HARVESTERS = {
    "olx": lambda t, q, use_chrome, chrome_path, timeout: harvest_olx(t, q, use_chrome=use_chrome, chrome_path=chrome_path, timeout=timeout),
    "rozetka": lambda t, q, _uc, _cp, timeout: harvest_rozetka(t, q, timeout=timeout),
    "prom": lambda t, q, _uc, _cp, timeout: harvest_prom(t, q, timeout=timeout),
    "hotline": lambda t, q, _uc, _cp, timeout: harvest_hotline(t, q, timeout=timeout),
    "aliexpress": lambda t, q, _uc, _cp, timeout: harvest_aliexpress(t, q, timeout=timeout),
    "idealo_de": lambda t, q, _uc, _cp, timeout: harvest_idealo_de(t, q, timeout=timeout),
    "ceneo_pl": lambda t, q, _uc, _cp, timeout: harvest_ceneo_pl(t, q, timeout=timeout),
    "amazon_de": lambda t, q, _uc, _cp, timeout: harvest_amazon_de(t, q, timeout=timeout),
}


def run_task_harvest(
    task: dict[str, Any],
    *,
    use_chrome: bool = False,
    chrome_path: Optional[str] = None,
    timeout: float = 15.0,
    max_workers: int = 4,
) -> dict[str, Any]:
    channels = [str(c).strip().lower() for c in (task.get("channels") or ["olx", "rozetka", "prom", "hotline"])]
    sub_queries = task.get("search_queries") or []
    if not sub_queries:
        sub_queries = []

    def _query_for_channel(ch: str, explicit_q: Optional[str] = None) -> str:
        if explicit_q:
            return _sanitize_query(explicit_q)
        if ch in ("idealo_de", "amazon_de"):
            return _select_query(task, lang="de", prefer_mpn=bool(task.get("canonical_mpns")))
        if ch == "ceneo_pl":
            return _select_query(task, lang="pl", prefer_mpn=bool(task.get("canonical_mpns")))
        if ch == "aliexpress":
            return _select_query(task, lang="en", prefer_mpn=False)
        return _select_query(task, lang="uk", prefer_mpn=False)

    jobs: list[tuple[str, str]] = []
    for ch in channels:
        if ch not in CHANNEL_HARVESTERS:
            continue
        if sub_queries:
            for sq in sub_queries:
                jobs.append((ch, _query_for_channel(ch, str(sq))))
        else:
            jobs.append((ch, _query_for_channel(ch)))

    offers: list[dict[str, Any]] = []
    manual_links: list[dict[str, Any]] = []
    seen_offer_keys: set[str] = set()
    seen_manual_urls: set[str] = set()
    channel_summary: dict[str, dict[str, Any]] = {
        ch: {"status": "PENDING", "offers_count": 0, "manual_links_count": 0}
        for ch in channels
    }

    def _run_job(job: tuple[str, str]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        ch, q = job
        fn = CHANNEL_HARVESTERS[ch]
        offs, mls = fn(task, q, use_chrome, chrome_path, timeout)
        return ch, offs, mls

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(jobs) or 1))) as pool:
        for ch, offs, mls in pool.map(_run_job, jobs):
            for off in offs:
                key = f"{off.get('channel')}|{off.get('url')}"
                if key not in seen_offer_keys:
                    seen_offer_keys.add(key)
                    offers.append(off)
                    channel_summary[ch]["offers_count"] += 1
            for ml in mls:
                u = ml.get("direct_search_url", "")
                if u and u not in seen_manual_urls:
                    seen_manual_urls.add(u)
                    manual_links.append(ml)
                    channel_summary[ch]["manual_links_count"] += 1

    for ch, stat in channel_summary.items():
        if stat["offers_count"] > 0:
            stat["status"] = "VERIFIED"
        elif stat["manual_links_count"] > 0:
            stat["status"] = "MANUAL_CHECK_LINK"
        else:
            stat["status"] = "EMPTY"

    return {
        "task_id": task.get("task_id", ""),
        "raw_query": task.get("raw_query", ""),
        "category": task.get("category", "general"),
        "canonical_mpns": task.get("canonical_mpns") or [],
        "queries_i18n": task.get("queries_i18n") or {},
        "allowed_conditions": task.get("allowed_conditions") or ["brand_new", "open_box", "used_good", "refurbished"],
        "max_budget_uah": task.get("max_budget_uah"),
        "target_price_uah": task.get("target_price_uah"),
        "urgency_days": task.get("urgency_days", 14),
        "estimated_weight_kg": task.get("estimated_weight_kg", 1.5),
        "volumetric_factor": task.get("volumetric_factor", 1.0),
        "customs_mode": task.get("customs_mode", "standard_150eur"),
        "pickup_regions": task.get("pickup_regions") or [],
        "require_local_pickup": bool(task.get("require_local_pickup", False)),
        "min_ram_gb": task.get("min_ram_gb"),
        "channels": channels,
        "offers": offers,
        "manual_links": manual_links,
        "channel_summary": channel_summary,
    }


def detect_saved_page_channel(html_text: str, filename: str = "") -> str:
    low_html = (html_text or "").lower()
    low_name = (filename or "").lower()
    combined = f"{low_name}\n{low_html}"

    if "olx.ua" in combined or 'data-cy="l-card"' in low_html or "data-cy='l-card'" in low_html:
        return "olx"
    if "aliexpress.com" in combined or "/item/" in combined:
        return "aliexpress"
    if "prom.ua" in combined:
        return "prom"
    if "hotline.ua" in combined:
        return "hotline"
    if "amazon.de" in combined or "/dp/" in combined:
        return "amazon_de"
    if "rozetka.com.ua" in combined:
        return "rozetka"
    if "idealo.de" in combined:
        return "idealo_de"
    if "ceneo.pl" in combined:
        return "ceneo_pl"
    return "unknown"


def _parse_rozetka_saved_html(
    html_text: str,
    task: dict[str, Any],
    search_url: str,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    if not html_text or is_antibot_challenge(200, html_text):
        return [], _make_manual_link(
            task,
            channel="rozetka",
            route_country="UA",
            currency="UAH",
            reason_status="ANTIBOT_OR_EMPTY",
            direct_search_url=search_url,
        )
    dom = parse_dom(html_text)
    offers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    task_id = task.get("task_id", "")

    for idx, a in enumerate(dom.find_all("a", predicate=lambda n: bool(n.attr("href")))):
        href = a.attr("href").strip()
        full_url = f"https://rozetka.com.ua{href}" if href.startswith("/") else href
        if not is_direct_product_url(full_url, "rozetka") or full_url in seen_urls:
            continue
        title = a.get_text(" ", strip=True)
        if len(title) < 4:
            continue
        # Try to find a nearby price in the anchor text
        price_val = parse_price_value(title)
        if price_val is None or price_val <= 0:
            continue
        seen_urls.add(full_url)
        m_id = re.search(r"/p(\d+)", full_url)
        oid = f"rozetka_{m_id.group(1)}" if m_id else f"rozetka_saved_{idx}"
        offers.append(
            {
                "offer_id": oid,
                "task_id": task_id,
                "channel": "rozetka",
                "route_country": "UA",
                "store_name": "Rozetka.ua (Saved Page)",
                "trust_tier": 1,
                "title": title,
                "url": full_url,
                "image_url": "",
                "price_raw": price_val,
                "currency": "UAH",
                "shipping_raw": 0.0,
                "raw_price_snippet": f"{int(price_val) if price_val.is_integer() else price_val} ₴",
                "raw_condition": "Нове",
                "in_stock": True,
                "delivery_days": 1,
                "warranty_months": 24,
                "specs": {"source": "saved_browser_page"},
            }
        )
    if not offers:
        return [], _make_manual_link(
            task,
            channel="rozetka",
            route_country="UA",
            currency="UAH",
            reason_status="EMPTY_SAVED_HTML",
            direct_search_url=search_url,
        )
    return offers, None


def parse_saved_pages_dir(saved_dir: Path) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "SAVED_BROWSER_IMPORT",
        "raw_query": "Збережені сторінки з вашого залогіненого браузера (Ctrl+S)",
        "category": "general",
        "canonical_mpns": [],
        "queries_i18n": {},
        "allowed_conditions": ["brand_new", "open_box", "used_good", "refurbished"],
        "urgency_days": 14,
        "estimated_weight_kg": 1.5,
        "volumetric_factor": 1.0,
        "customs_mode": "standard_150eur",
        "pickup_regions": [],
        "require_local_pickup": False,
        "min_ram_gb": None,
        "channels": [],
    }

    offers: list[dict[str, Any]] = []
    manual_links: list[dict[str, Any]] = []
    seen_offer_keys: set[str] = set()
    seen_manual_urls: set[str] = set()
    channel_summary: dict[str, dict[str, Any]] = {}

    if not saved_dir.is_dir():
        return {
            **task,
            "offers": [],
            "manual_links": [],
            "channel_summary": {},
        }

    html_files = sorted(
        p
        for p in saved_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".html", ".htm")
    )

    for html_file in html_files:
        try:
            html_text = html_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        ch = detect_saved_page_channel(html_text, html_file.name)
        if ch == "unknown":
            continue

        if ch not in task["channels"]:
            task["channels"].append(ch)
        if ch not in channel_summary:
            channel_summary[ch] = {"status": "PENDING", "offers_count": 0, "manual_links_count": 0}

        source_ref = html_file.name
        parsed_offers: list[dict[str, Any]] = []
        parsed_ml: Optional[dict[str, Any]] = None

        if ch == "olx":
            parsed_offers, parsed_ml = parse_olx_html(html_text, task, f"https://www.olx.ua/uk/list/ ({source_ref})")
        elif ch == "aliexpress":
            parsed_offers, parsed_ml = parse_aliexpress_html(
                html_text, task, f"https://www.aliexpress.com/wholesale ({source_ref})"
            )
        elif ch == "prom":
            parsed_offers, parsed_ml = parse_prom_html(html_text, task, f"https://prom.ua/ua/search ({source_ref})")
        elif ch == "hotline":
            parsed_offers, parsed_ml = parse_hotline_html(html_text, task, f"https://hotline.ua/ua/sr/ ({source_ref})")
        elif ch == "amazon_de":
            parsed_offers, parsed_ml = parse_amazon_de_html(html_text, task, f"https://www.amazon.de/s ({source_ref})")
        elif ch == "idealo_de":
            parsed_offers, parsed_ml = parse_idealo_html(
                html_text, task, f"https://www.idealo.de/preisvergleich/ ({source_ref})"
            )
        elif ch == "ceneo_pl":
            parsed_offers, parsed_ml = parse_ceneo_html(html_text, task, f"https://www.ceneo.pl/ ({source_ref})")
        elif ch == "rozetka":
            parsed_offers, parsed_ml = _parse_rozetka_saved_html(
                html_text, task, f"https://rozetka.com.ua/ ({source_ref})"
            )

        for off in parsed_offers:
            specs = dict(off.get("specs") or {})
            specs.setdefault("saved_file", html_file.name)
            off["specs"] = specs
            key = f"{off.get('channel')}|{off.get('url')}"
            if key not in seen_offer_keys:
                seen_offer_keys.add(key)
                offers.append(off)
                channel_summary[ch]["offers_count"] += 1

        if parsed_ml and not parsed_offers:
            u = parsed_ml.get("direct_search_url", "")
            if u and u not in seen_manual_urls:
                seen_manual_urls.add(u)
                manual_links.append(parsed_ml)
                channel_summary[ch]["manual_links_count"] += 1

    for ch, stat in channel_summary.items():
        if stat["offers_count"] > 0:
            stat["status"] = "VERIFIED"
        elif stat["manual_links_count"] > 0:
            stat["status"] = "MANUAL_CHECK_LINK"
        else:
            stat["status"] = "EMPTY"

    return {
        **task,
        "offers": offers,
        "manual_links": manual_links,
        "channel_summary": channel_summary,
    }


def build_copy_to_antigravity_markdown(bundle: dict[str, Any]) -> str:
    lines = [
        "# 📦 Omni Residential IP Relay — Результати локального збору",
        f"- **Час генерації:** `{bundle.get('generated_at', '')}`",
        f"- **Режим IP:** `{bundle.get('source_ip_mode', 'residential_local')}`",
        f"- **Задач оброблено:** {len(bundle.get('tasks') or [])}",
        "",
        "> Скопіюйте весь цей файл (`Ctrl+A` → `Ctrl+C`) і вставте у чат Antigravity або завантажте `latest_relay_result.json`.",
        "",
    ]

    for t in bundle.get("tasks") or []:
        tid = t.get("task_id", "")
        raw_q = t.get("raw_query", "")
        lines.append(f"## 🎯 {tid}: {raw_q}")
        meta_parts = [f"Категорія: `{t.get('category', 'general')}`"]
        if t.get("pickup_regions"):
            meta_parts.append(f"Самовивіз: {', '.join(t['pickup_regions'])}")
        if t.get("min_ram_gb"):
            meta_parts.append(f"RAM ≥ {t['min_ram_gb']} ГБ")
        if t.get("target_price_uah"):
            meta_parts.append(f"Ціль: ≤ {round(float(t['target_price_uah']))} ₴")
        lines.append(f"- {' | '.join(meta_parts)}")

        summary = t.get("channel_summary") or {}
        if summary:
            ch_badges = []
            for ch, st in summary.items():
                status = st.get("status", "")
                cnt = st.get("offers_count", 0)
                icon = "✅" if status == "VERIFIED" else ("🔗" if status == "MANUAL_CHECK_LINK" else "⚪")
                ch_badges.append(f"{icon} `{ch}` ({cnt})")
            lines.append(f"- **Канали:** {' · '.join(ch_badges)}")
        lines.append("")

        offers = t.get("offers") or []
        if offers:
            lines.append("### Прямі пропозиції (товарні картки)")
            for idx, off in enumerate(offers[:25], start=1):
                price_raw = off.get("price_raw", 0)
                cur = off.get("currency", "UAH")
                price_str = f"{int(price_raw) if float(price_raw).is_integer() else price_raw} {cur}"
                specs = off.get("specs") or {}
                welcome_tag = ""
                if specs.get("is_welcome_deal_detected") and specs.get("est_existing_price_usd") is not None:
                    est_p = float(specs["est_existing_price_usd"])
                    welcome_tag = f" (Welcome Deal; для існуючого акаунта орієнтовно ~${est_p:.2f} до купонів)"
                loc = off.get("location") or specs.get("location") or ""
                loc_str = f" · 📍 {loc}" if loc else ""
                cond_str = f" · {off.get('raw_condition')}" if off.get("raw_condition") else ""
                lines.append(
                    f"{idx}. **[{off.get('channel')}]** [{off.get('title')}]({off.get('url')}) — "
                    f"**{price_str}**{welcome_tag} ({off.get('store_name', '')}{loc_str}{cond_str})"
                )
            lines.append("")
        else:
            lines.append("_Прямих карток не знайдено (див. ручні посилання нижче)._")
            lines.append("")

        mls = t.get("manual_links") or []
        if mls:
            lines.append("### Посилання для ручної перевірки (WAF / Anti-bot / Резерв)")
            for ml in mls:
                lines.append(
                    f"- **{ml.get('channel')}** (`{ml.get('reason_status')}`): "
                    f"{ml.get('breakeven_hint_uk', '')} → [Відкрити пошук]({ml.get('direct_search_url')})"
                )
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _load_dotenv_vars(search_dirs: list[Path]) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    for d in search_dirs:
        env_file = d / ".env"
        if not env_file.is_file():
            continue
        try:
            for raw_line in env_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in env_vars:
                    env_vars[k] = v
        except Exception:
            pass
    return env_vars


def push_bundle_to_webhook(bundle: dict[str, Any], webhook_url: str, sync_secret: str = "", timeout: float = 20.0) -> bool:
    if not webhook_url:
        return False
    payload = {
        "action": "ingest_relay_bundle",
        "secret": sync_secret,
        "bundle": bundle,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception as exc:
        print(f"[relay] Webhook push warning: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Offline Self-Test (--self-test, zero network)
# ---------------------------------------------------------------------------

def run_self_test() -> int:
    print("[relay] Running offline --self-test on embedded HTML/JSON fixtures...")

    sample_task = {
        "task_id": "SELFTEST-001",
        "raw_query": 'Монітор 43" LG 43UN700-B',
        "category": "monitors_large_format",
        "pickup_regions": ["Київ", "Житомир"],
        "require_local_pickup": True,
        "target_price_uah": 18500,
    }

    # 1. OLX HTML fixture
    olx_html = """
    <html><body>
      <div data-cy="l-card">
        <a href="/d/uk/obyavlenie/monitor-lg-43un700-kyiv-IDTest123.html"><h4>Монітор LG 43UN700-B 4K IPS</h4></a>
        <p>13 500 грн</p>
        <span>Вживане</span>
        <span>Київ, Печерський - Сьогодні</span>
      </div>
    </body></html>
    """
    olx_offers, olx_ml = parse_olx_html(olx_html, sample_task, "https://www.olx.ua/uk/list/ko/q-LG/")
    assert len(olx_offers) == 1, f"Expected 1 OLX offer, got {len(olx_offers)}"
    assert olx_offers[0]["url"].endswith("/d/uk/obyavlenie/monitor-lg-43un700-kyiv-IDTest123.html")
    assert olx_offers[0]["price_raw"] == 13500.0
    assert olx_offers[0]["location"] == "Київ, Печерський - Сьогодні"
    assert olx_offers[0]["raw_condition"] == "Вживане"
    assert olx_ml is None

    # 2. Rozetka goods payload fixture
    rz_goods = [
        {
            "id": 389102002,
            "title": 'Монітор 42.5" LG 43SQ700S-W 4K IPS',
            "price": 18999,
            "price_pcs": "18 999 ₴",
            "href": "https://rozetka.com.ua/ua/lg-43sq700s-w/p389102002/",
            "seller_id": 5,
            "sell_status": "available",
            "product": {"state": "new"},
        }
    ]
    rz_offers, rz_ml = parse_rozetka_goods(rz_goods, sample_task, "https://rozetka.com.ua/ua/search/?text=LG")
    assert len(rz_offers) == 1
    assert rz_offers[0]["trust_tier"] == 1
    assert rz_offers[0]["store_name"] == "Rozetka.ua"
    assert rz_ml is None

    # 3. Prom HTML fixture
    prom_html = """
    <html><body>
      <div data-qaid="product_block">
        <a data-qaid="product_link" href="/ua/p9994301-lg-43sq700s.html">
          <span data-qaid="product_name">Монітор LG 43SQ700S-W 4K</span>
        </a>
        <span data-qaid="product_price">17 800 ₴</span>
        <span data-qaid="company_name">TechShop UA</span>
        <span data-qaid="product_presence">В наявності</span>
      </div>
    </body></html>
    """
    prom_offers, prom_ml = parse_prom_html(prom_html, sample_task, "https://prom.ua/ua/search?search_term=LG")
    assert len(prom_offers) == 1
    assert prom_offers[0]["url"] == "https://prom.ua/ua/p9994301-lg-43sq700s.html"
    assert prom_offers[0]["price_raw"] == 17800.0
    assert prom_ml is None

    # 4. Hotline JSON-LD fixture
    hotline_html = """
    <html><head>
      <script type="application/ld+json">
        {"@type": "Product", "name": "LG 43UN700-B", "url": "https://hotline.ua/ua/computer-monitory/lg-43un700-b/", "offers": {"lowPrice": "21999", "offerCount": 7, "priceCurrency": "UAH"}}
      </script>
    </head><body></body></html>
    """
    hl_offers, hl_ml = parse_hotline_html(hotline_html, sample_task, "https://hotline.ua/ua/sr/?q=LG")
    assert len(hl_offers) == 1
    assert hl_offers[0]["price_raw"] == 21999.0
    assert hl_offers[0]["orders_or_reviews"] == 7
    assert hl_ml is None

    # 5. AliExpress JSON + Welcome Deal un-masking + x5sec fail-closed fallback
    ali_good_html = (
        '<script>window._init_data_={"mods":{"itemList":{"content":[{"productId":"100500777",'
        '"title":{"displayTitle":"SMLIGHT SLZB-06 Zigbee PoE Coordinator"},'
        '"prices":{"salePrice":{"minPrice":29.5,"formattedPrice":"$29.50","currencyCode":"USD"}},'
        '"store":{"storeName":"SMLIGHT Official Store"},'
        '"sellingPoints":[{"tagContent":{"tagText":"Choice"}},{"tagContent":{"tagText":"Free shipping"}}]}]}}};</script>'
    )
    ali_offers, ali_ml = parse_aliexpress_html(ali_good_html, sample_task, "https://www.aliexpress.com/wholesale?SearchText=SLZB-06")
    assert len(ali_offers) == 1
    assert ali_offers[0]["url"] == "https://www.aliexpress.com/item/100500777.html"
    assert ali_ml is None

    ali_welcome_html = (
        '<html><body><a href="https://www.aliexpress.com/item/100500999888.html">'
        'ALIENTEK T90B Soldering Iron 140W US $33.09 New shoppers - US $8.13 500+ sold Free shipping'
        '</a></body></html>'
    )
    ali_w_offers, ali_w_ml = parse_aliexpress_html(
        ali_welcome_html, sample_task, "https://www.aliexpress.com/wholesale?SearchText=T90B"
    )
    assert len(ali_w_offers) == 1
    assert ali_w_offers[0]["price_raw"] == 33.09
    assert ali_w_offers[0]["specs"]["is_welcome_deal_detected"] is True
    assert ali_w_offers[0]["specs"]["welcome_price_usd"] == 33.09
    assert ali_w_offers[0]["specs"]["welcome_discount_usd"] == 8.13
    assert ali_w_offers[0]["specs"]["est_existing_price_usd"] == 35.74
    assert ali_w_ml is None

    ali_waf_html = "<html><body><script>var x5sec = 'punish_token';</script></body></html>"
    ali_waf_offers, ali_waf_ml = parse_aliexpress_html(ali_waf_html, sample_task, "https://www.aliexpress.com/wholesale?SearchText=SLZB-06")
    assert ali_waf_offers == []
    assert ali_waf_ml is not None
    assert ali_waf_ml["reason_status"] == "ANTIBOT_CHALLENGE_PUNISH"

    # 6. Markdown bundle generation
    bundle = {
        "schema_version": "1.0",
        "generated_at": "2026-11-25T16:40:00Z",
        "source_ip_mode": "residential_local",
        "tasks": [
            {
                **sample_task,
                "offers": olx_offers + rz_offers + prom_offers + hl_offers + ali_offers + ali_w_offers,
                "manual_links": [ali_waf_ml],
                "channel_summary": {
                    "olx": {"status": "VERIFIED", "offers_count": 1},
                    "rozetka": {"status": "VERIFIED", "offers_count": 1},
                    "prom": {"status": "VERIFIED", "offers_count": 1},
                    "hotline": {"status": "VERIFIED", "offers_count": 1},
                    "aliexpress": {"status": "MANUAL_CHECK_LINK", "offers_count": 1},
                },
            }
        ],
    }
    md = build_copy_to_antigravity_markdown(bundle)
    assert "SELFTEST-001" in md
    assert "https://www.olx.ua/d/uk/obyavlenie/monitor-lg-43un700-kyiv-IDTest123.html" in md
    assert "ANTIBOT_CHALLENGE_PUNISH" in md
    assert "Welcome Deal; для існуючого акаунта орієнтовно ~$35.74 до купонів" in md

    print("[relay] --self-test PASSED (OLX, Rozetka, Prom, Hotline, AliExpress + Welcome Deal + x5sec fail-closed, Markdown).")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        prog="omni_residential_harvester",
        description="Zero-dependency Residential IP Relay Harvester for Omni Shopping Agent",
    )
    parser.add_argument(
        "--tasks",
        default=str(script_dir / "relay_tasks.json"),
        help="Path to relay_tasks.json manifest",
    )
    parser.add_argument(
        "--output-dir",
        default=str(script_dir / "output"),
        help="Output directory for relay JSON and COPY_TO_ANTIGRAVITY.md",
    )
    parser.add_argument(
        "--saved-dir",
        default=str(script_dir / "saved_pages"),
        help="Directory with manually saved Chrome Ctrl+S HTML/HTM pages",
    )
    parser.add_argument(
        "--parse-saved",
        action="store_true",
        help="Parse only saved_pages/*.html|*.htm without hitting the network",
    )
    parser.add_argument("--chrome", action="store_true", help="Allow local Chrome/Edge --headless=new --dump-dom for OLX if available")
    parser.add_argument("--webhook-url", default="", help="Optional Google Apps Script / webhook URL for direct push")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    parser.add_argument("--max-workers", type=int, default=4, help="Parallel channel workers per task")
    parser.add_argument("--self-test", action="store_true", help="Run offline parser sanity checks and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    saved_dir = Path(args.saved_dir)
    started = time.time()
    harvested_tasks: list[dict[str, Any]] = []

    if args.parse_saved:
        print(f"[relay] Parsing manually saved browser pages from {saved_dir} (offline mode)...")
        saved_bundle_task = parse_saved_pages_dir(saved_dir)
        harvested_tasks.append(saved_bundle_task)
        print(
            f"[relay]   -> {len(saved_bundle_task['offers'])} direct offers, "
            f"{len(saved_bundle_task['manual_links'])} manual check link(s)."
        )
    else:
        tasks_path = Path(args.tasks)
        if not tasks_path.is_file():
            print(f"[relay] ERROR: Tasks file not found: {tasks_path}", file=sys.stderr)
            return 1

        raw_manifest = json.loads(tasks_path.read_text(encoding="utf-8"))
        raw_tasks = raw_manifest.get("tasks") if isinstance(raw_manifest, dict) else raw_manifest
        if not isinstance(raw_tasks, list) or not raw_tasks:
            print("[relay] ERROR: No tasks found in manifest.", file=sys.stderr)
            return 1

        chrome_path = _find_chrome_path() if args.chrome else None
        if args.chrome:
            print(f"[relay] Local browser for DOM dump: {chrome_path or 'not found (HTTP fallback)'}")

        for t in raw_tasks:
            if not isinstance(t, dict):
                continue
            print(f"[relay] Harvesting {t.get('task_id', 'TASK')}: {t.get('raw_query', '')} ...")
            res = run_task_harvest(
                t,
                use_chrome=args.chrome,
                chrome_path=chrome_path,
                timeout=args.timeout,
                max_workers=args.max_workers,
            )
            harvested_tasks.append(res)
            print(
                f"[relay]   -> {len(res['offers'])} direct offers, {len(res['manual_links'])} manual check link(s)."
            )

        if saved_dir.is_dir():
            has_saved_html = any(
                p.is_file() and p.suffix.lower() in (".html", ".htm")
                for p in saved_dir.iterdir()
            )
            if has_saved_html:
                print(f"[relay] Found saved browser HTML pages in {saved_dir}, parsing...")
                saved_bundle_task = parse_saved_pages_dir(saved_dir)
                if saved_bundle_task["offers"] or saved_bundle_task["manual_links"]:
                    harvested_tasks.append(saved_bundle_task)
                    print(
                        f"[relay]   -> SAVED_BROWSER_IMPORT: {len(saved_bundle_task['offers'])} direct offers, "
                        f"{len(saved_bundle_task['manual_links'])} manual check link(s)."
                    )

    now_utc = dt.datetime.now(dt.timezone.utc)
    bundle = {
        "schema_version": "1.0",
        "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_ip_mode": "saved_browser_html" if args.parse_saved else "residential_local",
        "elapsed_sec": round(time.time() - started, 2),
        "tasks": harvested_tasks,
    }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_name = now_utc.strftime("%Y%m%d_%H%M%S")
    timestamped_json = out_dir / f"relay_result_{ts_name}.json"
    latest_json = out_dir / "latest_relay_result.json"
    md_path = out_dir / "COPY_TO_ANTIGRAVITY.md"

    json_text = json.dumps(bundle, ensure_ascii=False, indent=2)
    timestamped_json.write_text(json_text, encoding="utf-8")
    latest_json.write_text(json_text, encoding="utf-8")

    md_text = build_copy_to_antigravity_markdown(bundle)
    md_path.write_text(md_text, encoding="utf-8")

    env_vars = _load_dotenv_vars([script_dir, script_dir.parent, Path.cwd()])
    webhook_url = (
        args.webhook_url
        or os.environ.get("GSHEETS_WEBHOOK_URL", "")
        or os.environ.get("OMNI_GSHEETS_WEBAPP_URL", "")
        or env_vars.get("GSHEETS_WEBHOOK_URL", "")
        or env_vars.get("OMNI_GSHEETS_WEBAPP_URL", "")
    ).strip()
    sync_secret = (
        os.environ.get("OMNI_GSHEETS_SYNC_SECRET", "")
        or env_vars.get("OMNI_GSHEETS_SYNC_SECRET", "")
    ).strip()
    if webhook_url:
        pushed = push_bundle_to_webhook(bundle, webhook_url, sync_secret=sync_secret, timeout=args.timeout)
        print(f"[relay] Webhook push: {'OK' if pushed else 'FAILED (local files still saved)'}")

    print(f"[relay] Saved JSON: {latest_json}")
    print(f"[relay] Saved Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
