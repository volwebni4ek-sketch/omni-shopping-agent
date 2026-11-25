from __future__ import annotations

import re
import unicodedata
from typing import Optional

NOISE_PATTERNS = [
    r"\bакція\b",
    r"\bскидка\b",
    r"\bзнижка\b",
    r"\bтоп продажів\b",
    r"\bтоп продаж\b",
    r"\bхіт\b",
    r"\bхит\b",
    r"\bновинка\b",
    r"\bsuper price\b",
    r"\bhot price\b",
    r"\bподарунок\b",
    r"\bподарок\b",
    r"\bрозпродаж\b",
    r"\bраспродажа\b",
    r"\bв наявності\b",
    r"\bв наличии\b",
    r"\bоригінал\b",
    r"\bоригинал\b",
    r"\b100%(?!\w)",
    r"\bофіційн(?:ий|а|е|і)\b",
    r"\bофициальн(?:ый|ая|ое|ые)\b",
    r"\bгарантія\s*\d+\s*(?:міс|мес|років|года|м)?\b",
    r"\bгарантия\s*\d+\s*(?:міс|мес|років|года|м)?\b",
    r"\bбезкоштовна доставка\b",
    r"\bбесплатная доставка\b",
    r"\bдоставка 1-2 дні\b",
    r"\bпром[\-\s]?оплата\b",
    r"\bolx\s+доставка\b",
    r"\[(?:id\s*)?[a-zA-Z0-9_\-\.\/]+\]",
    r"\((?:код\s*товару|код\s*товара|id|артикул|арт\.?)\s*:?[a-zA-Z0-9_\-\s]+\)",
    r"\bкод\s*(?:товару|товара)\s*:?\s*[a-zA-Z0-9_\-]+",
    r"\bарт(?:икул)?\.?\s*:?\s*[a-zA-Z0-9_\-]+",
]

COMPILED_NOISE_REGEX = [re.compile(p, re.IGNORECASE) for p in NOISE_PATTERNS]

TECH_SPEC_BRACKET_RE = re.compile(
    r"(?:"
    r"\d+(?:[.,]\d+)?\s*(?:kWh|Wh|mAh|Hz|ms|dB|W|V|кВт\*год|кВт·год|кВт|Вт\*год|Вт·год|Вт|мАг|мАч|Гц|мс|дБ|В)"
    r"(?![a-zA-Zа-яіїєґА-ЯІЇЄҐ0-9])"
    r"|LiFePO4"
    r")",
    re.IGNORECASE,
)

# Tokens that represent physical units, resolutions, or interface standards rather than MPNs
UNIT_OR_SPEC_TOKEN_RE = re.compile(
    r"^(?:"
    r"\d+(?:[.,]\d+)?(?:W|WH|KW|KWH|HZ|KHZ|MHZ|GHZ|GB|TB|MB|KB|MM|CM|DM|M|KM|KG|G|MG|ML|L|MAH|AH|V|MV|KV|A|MA|INCH|IN|FPS|MS|DB|RPM|NITS|NIT|PPI|DPI|MP|NM|BAR|PSI|BTU|LM|LUX)"
    r"|[248]K"
    r"|\d{3,4}[PI]"
    r"|(?:USB|HDMI|DP|DDR|GDDR|SATA|PCIE|WIFI|BT|GEN)\d+[A-Z]?"
    r"|(?:WIN|IOS|ANDROID)\d+"
    r"|MP[34]"
    r"|H26[45]"
    r")$",
    re.IGNORECASE,
)

# Candidate MPN token: ASCII alphanumeric with optional internal '-', '.', '/'
MPN_TOKEN_RE = re.compile(r"\b([A-Za-z0-9]+(?:[.\-/][A-Za-z0-9]+)*)\b")

# Spaced MPN like "ECAM 22.110" or "ECAM 22.110.B" (second part contains a dot so brand+year like "LG 2026" is avoided)
SPACED_MPN_RE = re.compile(r"\b([A-Z]{2,6})\s+(\d{2,4}\.\d{2,4}(?:\.[A-Z0-9]{1,3})?)\b", re.IGNORECASE)

ACCESSORY_PATTERNS = [
    r"\bкронштейн\w*",
    r"\bкріпленн\w*",
    r"\bкабель\b",
    r"\bкабелі\b",
    r"\bкабеля\b",
    r"\bблок живлення\b",
    r"\bматриця для\b",
    r"\bчохол\b",
    r"\bчехол\b",
    r"\bплівка\b",
    r"\bпленка\b",
    r"\bручка\b",
    r"\bфільтр\b",
    r"\bфильтр\b",
    r"\bзапчастин\w*",
    r"\bпо запчастинах\b",
    r"\bпо запчастям\b",
    r"\bзапчасти\b",
    r"\bcable\b",
    r"\bmount\b",
    r"\bwall mount\b",
    r"\bstand only\b",
    r"\bprotective film\b",
    r"\bscreen protector\b",
    r"\bpower adapter\b",
    r"\bersatzteil\w*",
    r"\buchwyt\w*",
    r"\bkabel\b",
    r"\bzasilacz\b",
    r"\bfolia ochronna\b",
    r"\bcarrying case\b",
    r"\bкоробка від\b",
    r"\bінструкція\b",
]

KIT_TRAP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:solo|sologer[aä]t)\b", re.IGNORECASE), "Kit trap: Solo unit without battery/charger"),
    (re.compile(r"\b(?:body\s+only|tool\s+only|bare\s+tool)\b", re.IGNORECASE), "Kit trap: Body/tool only without battery/accessories"),
    (re.compile(r"\bohne\s+akku\b", re.IGNORECASE), "Kit trap: Ohne Akku (without battery)"),
    (re.compile(r"\bbez\s+akumulatora\b", re.IGNORECASE), "Kit trap: Bez akumulatora (without battery)"),
    (re.compile(r"\bбез\s+(?:акумулятора|акб|батареї|зарядного)\b", re.IGNORECASE), "Kit trap: Без акумулятора / ЗП"),
    (re.compile(r"\b110\s*v\s*(?:only)?\b", re.IGNORECASE), "Voltage trap: 110V US standard (incompatible with 230V UA grid without step-down transformer)"),
    (re.compile(r"\bus\s*plug(?:\s*110\s*v)?\b", re.IGNORECASE), "Plug/Voltage trap: US plug 110V"),
    (re.compile(r"\bамериканськ(?:а|ою)\s+вилк\w*", re.IGNORECASE), "Plug trap: US plug"),
]

STOPWORDS = {
    "кавомашина", "кофемашина", "кавоварка", "кофеварка", "монітор", "монитор",
    "телевізор", "телевизор", "зарядна", "станція", "станция", "купити", "оригінал",
    "оригинал", "терміново", "срочно", "ціна", "цена", "новий", "нова", "нове",
    "продам", "продаж", "для", "від", "про", "при", "під", "над", "the", "and",
    "with", "for", "monitor", "gaming", "black", "white", "silver", "grey", "gray",
}


def normalize_title(raw_title: str) -> str:
    """Strip marketing noise, store codes, and redundant punctuation."""
    if not raw_title:
        return ""

    text = unicodedata.normalize("NFC", raw_title)

    for pattern in COMPILED_NOISE_REGEX:
        text = pattern.sub(" ", text)

    category_prefixes = [
        r"^навушники\s+(?:бездротові\s+|повнорозмірні\s+|bluetooth\s+)?",
        r"^наушники\s+(?:беспроводные\s+|полноразмерные\s+|bluetooth\s+)?",
        r"^кросівки\s+(?:бігові\s+|чоловічі\s+|жіночі\s+)?",
        r"^кроссовки\s+(?:беговые\s+|мужские\s+|женские\s+)?",
        r"^(?:портативна\s+|переносна\s+)?(?:зарядна\s+станція|зарядная\s+станция|електростанція|электростанция)\s+",
        r"^смартфон\s+",
        r"^телефон\s+",
        r"^ноутбук\s+",
    ]
    for cp in category_prefixes:
        text = re.sub(cp, "", text, flags=re.IGNORECASE)

    def clean_brackets(match: re.Match[str]) -> str:
        content = match.group(1).strip()
        if TECH_SPEC_BRACKET_RE.search(content):
            return f"({content})"
        if re.match(r"^[A-Z0-9\.\-\_]{6,}$", content) or re.search(r"\d{4,}", content):
            return ""
        return f"({content})"

    text = re.sub(r"\(([^)]+)\)", clean_brackets, text)
    text = re.sub(r"[\!\|\~]+|(?<![а-яіїєґa-z0-9])\*+|\*+(?![а-яіїєґa-z0-9])", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\-\,\.\s]+|[\-\,\.\s]+$", "", text).strip()
    return text


def extract_model_codes(title: str) -> list[str]:
    """
    Extract alphanumeric MPNs (e.g. EY905D10, ECAM22.110, U4323QE, 38WR85QC-W, 43UN700-B, DL24MP, C242, M32U).
    Filters out pure units/specs such as 100W, 60Hz, 32GB, 2400W, 4K, 2026.
    """
    if not title:
        return []

    codes: list[str] = []
    seen_canonical: set[str] = set()

    def _add_code(candidate: str) -> None:
        cleaned = candidate.strip("-./").upper()
        if not cleaned:
            return
        alnum = re.sub(r"[^A-Z0-9]", "", cleaned)
        if len(alnum) < 4:
            return
        if not (any(c.isalpha() for c in alnum) and any(c.isdigit() for c in alnum)):
            return
        # Reject pure unit/spec tokens (both raw and stripped)
        if UNIT_OR_SPEC_TOKEN_RE.match(cleaned) or UNIT_OR_SPEC_TOKEN_RE.match(alnum):
            return
        # Reject tokens that look like measurements with slash/dash e.g. 100-240V or 2048Wh/2400W
        parts = re.split(r"[\-/]", cleaned)
        if all(p.isdigit() or UNIT_OR_SPEC_TOKEN_RE.match(p) for p in parts if p):
            return
        if alnum not in seen_canonical:
            seen_canonical.add(alnum)
            codes.append(cleaned)

    # Check spaced MPNs like "ECAM 22.110" first
    for m in SPACED_MPN_RE.finditer(title):
        _add_code(f"{m.group(1)}{m.group(2)}")

    for m in MPN_TOKEN_RE.finditer(title):
        _add_code(m.group(1))

    return codes


def is_accessory_or_part(title: str, canonical_query: str = "") -> bool:
    """Detect cables, mounts, bags, cases, brackets, screen protectors, power adapters, and spare parts."""
    if not title:
        return False
    t_low = title.lower()
    q_low = (canonical_query or "").lower()

    for pat in ACCESSORY_PATTERNS:
        m = re.search(pat, t_low, flags=re.IGNORECASE)
        if m:
            matched_word = m.group(0)
            # Only treat as unwanted accessory if the canonical query itself didn't ask for it
            if not re.search(pat, q_low, flags=re.IGNORECASE) and matched_word not in q_low:
                return True
    return False


def detect_kit_trap(title: str) -> Optional[str]:
    """Detect solo/body-only/no-battery/110V kit traps and return a reason string if matched."""
    if not title:
        return None
    for pattern, reason in KIT_TRAP_PATTERNS:
        if pattern.search(title):
            return reason
    return None


def _tokenize_for_similarity(title: str) -> set[str]:
    cleaned = re.sub(r"[^\w\s\-/]", " ", title.lower())
    tokens: set[str] = set()
    for tok in cleaned.split():
        if len(tok) > 1 and tok not in STOPWORDS:
            tokens.add(tok)
    return tokens


def calculate_title_similarity(canonical_title: str, candidate_title: str) -> float:
    """Compute similarity score [0.0, 1.0] using MPN overlap + Jaccard token similarity."""
    if not canonical_title or not candidate_title:
        return 0.0

    if is_accessory_or_part(candidate_title, canonical_title):
        return 0.0

    can_codes = [re.sub(r"[^A-Z0-9]", "", c) for c in extract_model_codes(canonical_title)]
    cand_codes = [re.sub(r"[^A-Z0-9]", "", c) for c in extract_model_codes(candidate_title)]

    if can_codes and cand_codes:
        for cc in can_codes:
            for cd in cand_codes:
                if cc == cd or (len(cc) >= 5 and len(cd) >= 5 and (cc in cd or cd in cc)):
                    return 0.95
        # Both specify distinct MPNs with zero overlap -> likely different models
        return 0.20

    can_tokens = _tokenize_for_similarity(canonical_title)
    cand_tokens = _tokenize_for_similarity(candidate_title)
    if not can_tokens or not cand_tokens:
        return 0.0

    intersection = can_tokens & cand_tokens
    union = can_tokens | cand_tokens
    jaccard = len(intersection) / len(union)

    words_can = canonical_title.strip().split()
    brand_match = bool(words_can and words_can[0].lower() in candidate_title.lower())
    if brand_match and len(intersection) >= 2:
        return round(min(0.90, jaccard + 0.35), 4)

    return round(jaccard, 4)
