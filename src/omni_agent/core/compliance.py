from __future__ import annotations

import re


class ComplianceManager:
    """
    GDPR query sanitization and DOM price grounding verification.
    """

    EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
    PHONE_RE = re.compile(r"(?:\+?\d[\s\-\(\)]*){9,15}")
    CARD_RE = re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b")
    STREET_ADDRESS_RE = re.compile(
        r"(?:\b(?:вул\.|вулиця|ул\.|улица|просп\.|проспект|пров\.|бул\.|кв\.|офіс|дім|буд\.|str\.|ul\.|aleja|al\.)\s*[^\n,;]+(?:\d+[^\n,;]*)?)",
        re.IGNORECASE,
    )
    POSTAL_CITY_RE = re.compile(r"\b\d{5}\s+[А-ЯІЇЄҐA-Z][а-яіїєґa-z\-]{2,}\b")

    @classmethod
    def sanitize_gdpr_query(cls, query: str) -> str:
        """Strip emails, phone numbers, payment card numbers, and full street addresses before sending to extractors."""
        if not query:
            return ""

        sanitized = cls.EMAIL_RE.sub(" ", query)
        sanitized = cls.CARD_RE.sub(" ", sanitized)
        sanitized = cls.PHONE_RE.sub(" ", sanitized)
        sanitized = cls.STREET_ADDRESS_RE.sub(" ", sanitized)
        sanitized = cls.POSTAL_CITY_RE.sub(" ", sanitized)
        sanitized = re.sub(r"\s+", " ", sanitized).strip(" ,;-")
        return sanitized

    @classmethod
    def verify_price_grounding(cls, parsed_price: float, raw_price_snippet: str) -> tuple[bool, str]:
        """Verify that parsed_price matches a numeric price token inside raw_price_snippet."""
        if parsed_price <= 0:
            return False, "GROUNDING_INVALID_PRICE: parsed price must be positive"
        if not raw_price_snippet or not raw_price_snippet.strip():
            return False, "GROUNDING_MISSING_SNIPPET: raw_price_snippet is empty"

        # Normalize HTML entities (&nbsp;, &#160;) and Unicode whitespace (\u00a0, \u2009, \u202f)
        import html as _html

        cleaned = _html.unescape(raw_price_snippet)
        cleaned = re.sub(r"[\u00a0\u2007\u2009\u200a\u202f]", " ", cleaned)
        # Extract candidate numeric groups (with optional thousand spaces/dots/commas and decimals)
        raw_tokens = re.findall(r"\d+(?:[\s.,']\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?", cleaned)
        if not raw_tokens:
            return False, f"GROUNDING_NO_NUMBERS: no numeric tokens in '{raw_price_snippet}'"

        parsed_candidates: list[float] = []
        for tok in raw_tokens:
            val = cls._parse_numeric_token(tok)
            if val is not None:
                parsed_candidates.append(val)

        if not parsed_candidates:
            return False, f"GROUNDING_PARSE_FAILED: could not parse tokens in '{raw_price_snippet}'"

        # Allow 1.0 tolerance for rounding (e.g. 14999.0 vs 14999 or 149.99 vs 150)
        for cand in parsed_candidates:
            if abs(cand - parsed_price) <= 1.0:
                return True, f"GROUNDED: matched {cand} against parsed {parsed_price}"

        return False, (
            f"GROUNDING_MISMATCH: parsed {parsed_price} not found in snippet '{raw_price_snippet}' "
            f"(candidates={parsed_candidates})"
        )

    @staticmethod
    def _parse_numeric_token(token: str) -> float | None:
        s = token.strip().replace(" ", "").replace("'", "")
        if not s:
            return None
        # Handle both comma and dot present (e.g. "1,299.99" or "1.299,99")
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                # European format: 1.299,99
                s = s.replace(".", "").replace(",", ".")
            else:
                # US format: 1,299.99
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
                # Thousand separator like 1.299
                s = s.replace(".", "")
        try:
            return float(s)
        except ValueError:
            return None
