from __future__ import annotations

import re
from typing import Any, Optional

from omni_agent.core.models import ChannelTrustTier, NormalizedOffer, ShoppingTask
from omni_agent.core.normalizer import extract_ram_gb

TIER_A_BRANDS = {
    "dell", "lg", "apple", "sony", "bosch", "miele", "delonghi", "ecoflow",
    "bluetti", "anker", "samsung", "asus", "benq", "dyson", "makita", "dewalt",
    "philips", "braun", "kitchenaid", "eizo", "hp", "siemens", "garmin",
}

TIER_B_BRANDS = {
    "xiaomi", "acer", "lenovo", "aoc", "gigabyte", "msi", "jackery", "oukitel",
    "fossibot", "dabbsson", "rowenta", "tefal", "hisense", "tcl", "krups", "viewsonic",
}

MONITOR_KEYWORDS = (
    "монітор", "монитор", "monitor", "display", "43\"", "38\"", "42\"", "37.5\"",
    "40\"", "34\"", "ips", "uhd", "4k", "wqhd", "5k2k", "ultrawide", "oled",
    "comfortview", "flicker", "u4323qe", "43sq700s", "43un700", "38wr85qc",
)

POWER_STATION_KEYWORDS = (
    "зарядна станція", "зарядная станция", "power station", "lifepo4", "ecoflow",
    "bluetti", "jackery", "fossibot", "dabbsson", "oukitel", "delta", "ac200",
    "ups", "інвертор", "дбж", "kwh",
)

AI_MINI_PC_KEYWORDS = (
    "mac studio", "mac mini", "strix halo", "ai max", "395", "evo-x2", "ms-s1",
    "dgx spark", "unified memory", "міні-пк", "неттоп", "mini pc", "mini-pc",
    "framework desktop", "gb10", "m2 ultra", "m3 ultra", "m4 max", "m4 pro",
)

EYE_CARE_PRIORITY_KEYWORDS = (
    "eye", "оч", "зір", "комфорт", "comfort", "work", "робот", "офіс", "office",
    "код", "code", "програмув", "текст", "text", "static", "читан", "reading",
    "flicker", "tuv", "low blue", "ergo", "design", "дизайн", "монтаж",
)

GAMING_ONLY_KEYWORDS = (
    "gaming", "ігровий", "игровой", "esports", "кіберспорт", "240hz", "360hz",
)

LARGE_ASPECT_PRIORITY_KEYWORDS = (
    "кіно", "cinema", "movie", "aspect", "висот", "height", "16:9", "43", "42",
    "38", "40", "33", "large", "великий", "фокус", "focal", "4k", "5k2k", "wqhd+",
)


def _clamp(val: float, low: float = 0.0, high: float = 100.0) -> float:
    return round(max(low, min(high, float(val))), 1)


def _extract_diagonal_inches(title: str, specs: dict[str, Any]) -> Optional[float]:
    if specs.get("diagonal_inch") is not None:
        try:
            return float(specs["diagonal_inch"])
        except (TypeError, ValueError):
            pass
    if specs.get("screen_size_inch") is not None:
        try:
            return float(specs["screen_size_inch"])
        except (TypeError, ValueError):
            pass

    t = title or ""
    m = re.search(r"(\d{2}(?:[.,]\d)?)\s*(?:\"|''|дюйм\w*|inch(?:es)?)", t, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "."))

    # Infer well-known diagonal prefixes in monitor MPNs if explicit inch mark omitted
    t_up = t.upper()
    if re.search(r"\bU43\d{2}|\b43[A-Z]{2}\d{3}|\b42[A-Z]{2}", t_up):
        return 42.5 if "U43" in t_up else 43.0
    if re.search(r"\b38[A-Z]{2}\d{2}|\bU38\d{2}", t_up):
        return 37.5
    if re.search(r"\bU40\d{2}|\b40[A-Z]{2}\d{2}", t_up):
        return 39.7
    if re.search(r"\b34[A-Z]{2}\d{2}|\bU34\d{2}", t_up):
        return 34.0
    return None


class DomainSpecScorer:
    """
    Computes intrinsic quality_score (0..100), effective_quality_score (quality * condition.factor),
    and channel_score (0..100) for NormalizedOffer objects given a ShoppingTask.
    """

    @staticmethod
    def detect_profile(offer: NormalizedOffer, task: ShoppingTask) -> str:
        cat = (task.category or "").lower()
        combined = f"{cat} {task.raw_query} {task.human_notes} {offer.title}".lower()

        if "monitor" in cat or "монітор" in cat:
            return "monitors_large_format"
        if "power" in cat or "station" in cat or "станц" in cat:
            return "power_stations"
        if "ai_mini_pc" in cat or "mini_pc" in cat or "mini-pc" in cat or "міні-пк" in cat:
            return "ai_mini_pc"

        if any(kw in combined for kw in MONITOR_KEYWORDS):
            return "monitors_large_format"
        if any(kw in combined for kw in POWER_STATION_KEYWORDS) or re.search(r"\d+\s*k?wh\b", combined):
            return "power_stations"
        if any(kw in combined for kw in AI_MINI_PC_KEYWORDS):
            return "ai_mini_pc"
        return "general"

    @staticmethod
    def _brand_bonus(title: str, specs: dict[str, Any]) -> float:
        text = f"{specs.get('brand', '')} {title}".lower()
        if any(re.search(rf"\b{re.escape(b)}\b", text) for b in TIER_A_BRANDS):
            return 8.0
        if any(re.search(rf"\b{re.escape(b)}\b", text) for b in TIER_B_BRANDS):
            return 4.0
        return 0.0

    @staticmethod
    def _score_monitor(offer: NormalizedOffer, task: ShoppingTask) -> float:
        specs = offer.specs or {}
        title_low = (offer.title or "").lower()
        panel_str = f"{specs.get('panel_type', '')} {title_low}".lower()
        task_text = f"{task.raw_query} {task.human_notes} {task.category}".lower()

        score = 38.0

        # 1. Eye-Care & Panel Health
        is_gaming_only = any(k in task_text for k in GAMING_ONLY_KEYWORDS) and not any(
            k in task_text for k in EYE_CARE_PRIORITY_KEYWORDS
        )
        eye_health_prioritized = any(k in task_text for k in EYE_CARE_PRIORITY_KEYWORDS) or (
            not is_gaming_only
        )

        if "ips black" in panel_str:
            score += 18.0
        elif "oled" in panel_str or "qd-oled" in panel_str or "woled" in panel_str:
            if eye_health_prioritized:
                score -= 4.0
            else:
                score += 10.0
        elif "ips" in panel_str:
            score += 14.0
        elif re.search(r"\bva\b", panel_str) or "mva" in panel_str or "sva" in panel_str:
            score += 6.0

        # Hardware Low Blue Light / Flicker-Free / TUV / ComfortView Plus
        eye_care_markers = (
            "comfortview plus",
            "comfortview",
            "hardware low blue light",
            "low blue light",
            "tuv",
            "tüv",
            "flicker-free",
            "flicker free",
            "flicker-safe",
            "flicker safe",
            "eye care",
            "eyecare",
        )
        has_eye_care = (
            any(m in title_low for m in eye_care_markers)
            or bool(specs.get("comfortview_plus"))
            or bool(specs.get("flicker_free"))
            or bool(specs.get("tuv_certified"))
            or bool(specs.get("hardware_low_blue_light"))
        )
        if has_eye_care:
            score += 10.0

        # 2. Screen Diagonal & Aspect / Focal Comfort
        diag = _extract_diagonal_inches(offer.title, specs)
        aspect_str = f"{specs.get('aspect_ratio', '')} {specs.get('resolution', '')} {title_low}".lower()
        is_4k = any(k in aspect_str for k in ("4k", "uhd", "2160p", "3840x2160"))
        is_21_9 = any(k in aspect_str for k in ("21:9", "3440x1440", "3840x1600", "5120x2160", "wqhd", "5k2k", "ultrawide"))
        is_high_ultrawide = any(k in aspect_str for k in ("wqhd+", "5k2k", "3840x1600", "5120x2160"))

        wants_large_cinema = any(k in task_text for k in LARGE_ASPECT_PRIORITY_KEYWORDS) or (
            "monitors_large_format" in (task.category or "").lower()
        )

        if diag is not None:
            if 41.5 <= diag <= 44.0 and (is_4k or "16:9" in aspect_str or not is_21_9):
                score += 12.0
            elif 37.5 <= diag <= 40.5 and (is_high_ultrawide or is_21_9 or "5k" in aspect_str):
                score += 12.0
            elif 33.5 <= diag <= 34.9 and is_21_9:
                if wants_large_cinema:
                    score -= 8.0
            elif diag >= 31.5 and is_4k:
                score += 6.0

        # 3. Ergonomics & Connectivity (USB-C / KVM / Height-Adjustable Stand / Remote)
        ergo_markers = (
            "usb-c", "usb c", "type-c", "kvm", "height", "регулювання висоти",
            "регулировка высоты", "pivot", "пульт", "remote", "90w", "65w", "pd ",
        )
        has_ergo = (
            any(m in title_low for m in ergo_markers)
            or bool(specs.get("usb_c"))
            or bool(specs.get("kvm"))
            or bool(specs.get("height_adjustable"))
            or bool(specs.get("remote_control"))
        )
        if has_ergo:
            score += 6.0

        # 4. Brand, warranty, and social proof
        score += DomainSpecScorer._brand_bonus(offer.title, specs)
        if offer.warranty_months >= 36:
            score += 4.0
        elif offer.warranty_months >= 24:
            score += 2.0

        rating = offer.seller_rating or specs.get("rating")
        if rating is not None and float(rating) >= 4.6:
            score += 2.0

        return _clamp(score)

    @staticmethod
    def _score_power_station(offer: NormalizedOffer, task: ShoppingTask) -> float:
        specs = offer.specs or {}
        title_low = (offer.title or "").lower()
        chem = f"{specs.get('battery_chemistry', '')} {title_low}".lower()

        score = 45.0

        # Chemistry
        if "semi-solid" in chem or "напівтвердотіль" in chem:
            score += 20.0
        elif "lifepo4" in chem or "lfp" in chem:
            score += 18.0
        elif "nmc" in chem or "li-ion" in chem:
            score -= 5.0

        # Cycles
        cycles = specs.get("cycles_count")
        if cycles is None:
            m_cyc = re.search(r"(\d{3,4})\s*(?:\+?\s*)?(?:cycles|цикл)", title_low)
            cycles = int(m_cyc.group(1)) if m_cyc else None
        if cycles is not None:
            c_val = int(cycles)
            if c_val >= 4000:
                score += 8.0
            elif c_val >= 3000:
                score += 5.0
            elif c_val >= 2000:
                score += 2.0

        # Capacity Wh
        cap_wh = specs.get("capacity_wh")
        if cap_wh is None:
            m_kwh = re.search(r"(\d+(?:[.,]\d+)?)\s*kwh", title_low)
            m_wh = re.search(r"(\d+(?:[.,]\d+)?)\s*wh", title_low)
            if m_kwh:
                cap_wh = float(m_kwh.group(1).replace(",", ".")) * 1000.0
            elif m_wh:
                cap_wh = float(m_wh.group(1).replace(",", "."))
        if cap_wh is not None:
            wh = float(cap_wh)
            if wh >= 2000:
                score += 10.0
            elif wh >= 1000:
                score += 7.0
            elif wh >= 500:
                score += 4.0

        # UPS ms
        ups_ms = specs.get("ups_ms")
        if ups_ms is None:
            m_ups = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ms|мс)\b", title_low)
            if m_ups:
                ups_ms = float(m_ups.group(1).replace(",", "."))
        if ups_ms is not None:
            ms = float(ups_ms)
            if ms <= 10:
                score += 10.0
            elif ms <= 20:
                score += 7.0
            elif ms > 25:
                score -= 5.0

        # Noise dB
        noise_db = specs.get("noise_db")
        if noise_db is None:
            m_db = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:db|дб)\b", title_low)
            if m_db:
                noise_db = float(m_db.group(1).replace(",", "."))
        if noise_db is not None:
            db = float(noise_db)
            if db <= 35:
                score += 8.0
            elif db <= 45:
                score += 4.0
            elif db > 55:
                score -= 6.0

        score += DomainSpecScorer._brand_bonus(offer.title, specs)
        return _clamp(score)

    @staticmethod
    def _score_general(offer: NormalizedOffer, task: ShoppingTask) -> float:
        specs = offer.specs or {}
        score = 56.0

        # Brand reputation tier
        text = f"{specs.get('brand', '')} {offer.title}".lower()
        if any(re.search(rf"\b{re.escape(b)}\b", text) for b in TIER_A_BRANDS):
            score += 14.0
        elif any(re.search(rf"\b{re.escape(b)}\b", text) for b in TIER_B_BRANDS):
            score += 8.0
        else:
            score += 2.0

        # Warranty months
        if offer.warranty_months >= 36:
            score += 8.0
        elif offer.warranty_months >= 24:
            score += 6.0
        elif offer.warranty_months >= 12:
            score += 3.0
        elif offer.warranty_months == 0:
            score -= 5.0

        # Rating & review count
        rating = offer.seller_rating if offer.seller_rating is not None else specs.get("rating")
        if rating is not None:
            r = float(rating)
            if r >= 4.8:
                score += 8.0
            elif r >= 4.5:
                score += 5.0
            elif r >= 4.0:
                score += 2.0
            elif r < 3.5:
                score -= 6.0

        reviews = offer.orders_or_reviews or int(specs.get("reviews_count") or 0)
        if reviews >= 100:
            score += 6.0
        elif reviews >= 20:
            score += 4.0
        elif reviews >= 5:
            score += 2.0

        return _clamp(score)

    @staticmethod
    def _score_ai_mini_pc(offer: NormalizedOffer, task: ShoppingTask) -> float:
        specs = offer.specs or {}
        title_low = (offer.title or "").lower()
        combined = f"{title_low} {specs.get('chip', '')} {specs.get('cpu', '')} {specs.get('io', '')}".lower()

        score = 45.0

        # 1. Unified Memory / RAM Capacity
        ram_gb = specs.get("ram_gb")
        if ram_gb is not None:
            try:
                ram_val: Optional[int] = int(ram_gb)
            except (TypeError, ValueError):
                ram_val = extract_ram_gb(offer.title)
        else:
            ram_val = extract_ram_gb(offer.title)

        if ram_val is not None:
            if ram_val >= 192:
                score += 30.0
            elif ram_val >= 128:
                score += 26.0
            elif ram_val >= 96:
                score += 20.0
            elif ram_val >= 64:
                score += 15.0
            elif ram_val >= 48:
                score += 9.0
            elif ram_val >= 32:
                score += 4.0
            elif ram_val <= 24:
                score -= 8.0

        # 2. Architecture & Memory Bandwidth
        if any(k in combined for k in ("m3 ultra", "m2 ultra", "m1 ultra")):
            score += 16.0
        elif any(k in combined for k in ("m4 max", "m5 max", "m2 max", "m3 max")):
            score += 13.0
        elif any(k in combined for k in ("ai max+ 395", "ai max+ pro 495", "ai max 395", "strix halo")):
            score += 12.0
        elif any(k in combined for k in ("m4 pro", "m5 pro", "gb10", "dgx spark")):
            score += 10.0
        elif any(k in combined for k in ("hx 370", "8845hs")):
            score += 3.0

        # 3. Server / Media I/O
        if any(k in combined for k in ("10gbe", "10g lan", "10gbit", "10gbps")):
            score += 5.0
        if any(k in combined for k in ("thunderbolt 5", "usb4", "usb 4")):
            score += 4.0
        if any(k in combined for k in ("proxmox", "ecc", "pcie")):
            score += 3.0

        return _clamp(score, low=15.0, high=100.0)

    @classmethod
    def compute_quality_score(cls, offer: NormalizedOffer, task: ShoppingTask) -> float:
        profile = cls.detect_profile(offer, task)
        if profile == "monitors_large_format":
            return cls._score_monitor(offer, task)
        if profile == "power_stations":
            return cls._score_power_station(offer, task)
        if profile == "ai_mini_pc":
            return cls._score_ai_mini_pc(offer, task)
        return cls._score_general(offer, task)

    @staticmethod
    def compute_channel_score(offer: NormalizedOffer, urgency_days: int = 14) -> float:
        if not offer.in_stock:
            return 10.0

        if offer.trust_tier == ChannelTrustTier.TIER_1_OFFICIAL:
            score = 92.0
        elif offer.trust_tier == ChannelTrustTier.TIER_2_ESCROW:
            score = 78.0
        else:
            score = 50.0

        # Delivery days vs urgency_days
        if offer.delivery_days <= urgency_days:
            if offer.delivery_days <= 2:
                score += 4.0
            elif offer.delivery_days <= max(1, urgency_days // 2):
                score += 2.0
        else:
            delay = offer.delivery_days - urgency_days
            score -= min(20.0, delay * 2.0)

        # Warranty months
        if offer.warranty_months >= 36:
            score += 8.0
        elif 12 <= offer.warranty_months <= 24:
            score += 4.0
        elif offer.warranty_months > 24:
            score += 6.0
        elif offer.warranty_months == 0:
            score -= 8.0

        return _clamp(score)

    @classmethod
    def score_offer(cls, offer: NormalizedOffer, task: ShoppingTask) -> NormalizedOffer:
        q = cls.compute_quality_score(offer, task)
        eff_q = round(q * offer.condition.factor, 1)
        ch = cls.compute_channel_score(offer, urgency_days=task.urgency_days)

        offer.quality_score = q
        offer.effective_quality_score = eff_q
        offer.channel_score = ch
        return offer

    @classmethod
    def score_offers(cls, offers: list[NormalizedOffer], task: ShoppingTask) -> list[NormalizedOffer]:
        for offer in offers:
            cls.score_offer(offer, task)
        return offers
