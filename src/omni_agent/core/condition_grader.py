from __future__ import annotations

import re
from typing import Optional

from omni_agent.core.models import ConditionAssessment, ConditionCode


class ConditionGrader:
    """
    Analyzes listing title, description, and condition tags to assess physical condition,
    detect defective/for-parts items, Open Box / Amazon returns, refurbished units,
    and usage metrics (e.g. coffee cup count), returning a ConditionAssessment.
    """

    DEFECT_PATTERNS = [
        r"\bна запчастини\b",
        r"\bна запчасти\b",
        r"\bпід відновлення\b",
        r"\bпід ремонт\b",
        r"\bне вмикається\b",
        r"\bне працює\b",
        r"\bнесправн\w*",
        r"\bдефект\w*",
        r"\bбитий екран\b",
        r"\bбита матриця\b",
        r"\bрозбит\w*\s+(?:екран|матриц\w*)",
        r"\bсмуги на матриці\b",
        r"\bполосы на матрице\b",
        r"\bfor parts\b",
        r"\bnot working\b",
        r"\bcracked screen\b",
        r"\bbroken screen\b",
        r"\bdefekt\b",
        r"\bbastler\b",
        r"\buszkodzon\w*",
    ]

    OPEN_BOX_PATTERNS = [
        r"\bamazon\s*return\b",
        r"\bamazon\s*warehouse\b",
        r"\bretourenkauf\b",
        r"\bopen\s*box\b",
        r"\bопен\s*бокс\b",
        r"\bвітринн\w*",
        r"\bвитринн\w*",
        r"\bрозпакован\w*",
        r"\bраспакован\w*",
        r"\bстан нового\b",
        r"\bсостояние нового\b",
        r"\bяк нов\w*",
        r"\bкак нов\w*",
        r"\bідеальн\w*\s*стан\b",
        r"\bидеальн\w*\s*состоян\w*",
        r"\bповернення з європи\b",
        r"\bуцінка коробк\w*",
        r"\bпошкоджена упаковка\b",
        r"\blike new\b",
        r"\bwie neu\b",
        r"\bpowystawow\w*",
    ]

    REFURBISHED_PATTERNS = [
        r"\brefurbished\b",
        r"\bcertified renewed\b",
        r"\bamazon renewed\b",
        r"\brenewed\b",
        r"\bgeneralüberholt\b",
        r"\bodnowion\w*",
        r"\bреф\b",
        r"\bвідновлен\w*",
        r"\bвосстановлен\w*",
        r"\bдекальцинац\w*",
        r"\bпісля то\b",
        r"\bпосле то\b",
        r"\bпісля сервісу\b",
        r"\bобслужен\w*",
        r"\bперебран\w*",
    ]

    NEW_PATTERNS = [
        r"\bнов(ий|а|е|і)\b",
        r"\bнов(ый|ая|ое|ые)\b",
        r"\bbrand new\b",
        r"\bfactory sealed\b",
        r"\bneuware\b",
        r"\bnowy\b",
        r"\bзапечатан\w*",
        r"\bв заводській коробці\b",
        r"\bофіційна гарантія\b",
    ]

    USED_RAW_VALUES = {"вживане", "б/в", "б/у", "used", "gebraucht", "używany", "używane"}
    NEW_RAW_VALUES = {"нове", "новий", "new", "brand_new", "neu", "nowy"}

    @classmethod
    def grade(
        cls,
        title: str,
        description: str = "",
        raw_condition: str = "",
    ) -> ConditionAssessment:
        text = f"{title} {description} {raw_condition}".lower()
        raw_low = (raw_condition or "").strip().lower()

        # 1. Defective / For Parts
        for pat in cls.DEFECT_PATTERNS:
            if re.search(pat, text):
                code = ConditionCode.FOR_PARTS
                return ConditionAssessment(
                    code=code,
                    grade=2.0,
                    factor=code.condition_factor,
                    badge="⚠️ На запчастини / Дефект (2.0/10)",
                    summary="Товар з критичним дефектом або на запчастини",
                    usage_counter=None,
                )

        # 2. Extract usage counter (e.g. coffee cup count / shutter clicks)
        usage_counter: Optional[int] = None
        cup_match = re.search(r"пробіг\s*(\d+)\s*(?:чаш|порц)", text) or re.search(
            r"пробег\s*(\d+)\s*(?:чаш|порц)", text
        )
        if cup_match:
            try:
                usage_counter = int(cup_match.group(1))
            except ValueError:
                usage_counter = None

        # 3. Open Box / Warehouse / Showcase
        is_open_box = any(re.search(p, text) for p in cls.OPEN_BOX_PATTERNS)
        if usage_counter is not None and usage_counter <= 350 and not any(
            re.search(p, text) for p in cls.REFURBISHED_PATTERNS
        ):
            is_open_box = True

        if is_open_box:
            code = ConditionCode.OPEN_BOX
            grade = 9.5 if (usage_counter is not None and usage_counter <= 100) or "open box" in text or "warehouse" in text or "retourenkauf" in text else 9.2
            summary = "Преміальний стан: Open Box / вітринний зразок або повернення"
            if usage_counter is not None:
                summary += f" (пробіг {usage_counter} чашок)"
            return ConditionAssessment(
                code=code,
                grade=grade,
                factor=code.condition_factor,
                badge=f"✨ Open Box / Як новий ({grade:.1f}/10)",
                summary=summary,
                usage_counter=usage_counter,
            )

        # 4. Refurbished / Renewed / Serviced
        if any(re.search(p, text) for p in cls.REFURBISHED_PATTERNS):
            code = ConditionCode.REFURBISHED
            grade = 8.5
            return ConditionAssessment(
                code=code,
                grade=grade,
                factor=code.condition_factor,
                badge=f"🔧 Відновлений / Refurbished ({grade:.1f}/10)",
                summary="Відновлений виробником (Refurbished/Renewed) або пройшов повне сервісне ТО",
                usage_counter=usage_counter,
            )

        # 5. Brand New vs Used
        is_explicitly_used = raw_low in cls.USED_RAW_VALUES or any(
            w in text for w in ("вживан", "б/у", "б/в", "з дому", "бувший", "gebraucht", "używan")
        )
        is_explicitly_new = raw_low in cls.NEW_RAW_VALUES or any(
            re.search(p, text) for p in cls.NEW_PATTERNS
        )

        # If no used markers and no raw condition contradicting new, default new only when explicitly new or clean official channel
        if is_explicitly_new and not is_explicitly_used:
            code = ConditionCode.BRAND_NEW
            return ConditionAssessment(
                code=code,
                grade=10.0,
                factor=code.condition_factor,
                badge="📦 Новий (10.0/10)",
                summary="Новий товар у заводському пакуванні",
                usage_counter=None,
            )

        if not is_explicitly_used and raw_low == "" and usage_counter is None:
            # Clean catalog/retail listing without used indicators
            code = ConditionCode.BRAND_NEW
            return ConditionAssessment(
                code=code,
                grade=10.0,
                factor=code.condition_factor,
                badge="📦 Новий (10.0/10)",
                summary="Новий товар (без ознак вживаності)",
                usage_counter=None,
            )

        # 6. Regular Used in Good Condition
        code = ConditionCode.USED_GOOD
        grade = 7.5
        summary = "Вживаний товар у хорошому робочому стані"
        if usage_counter is not None:
            summary += f" (пробіг {usage_counter} чашок)"
            if usage_counter > 4000:
                grade = 6.8
        return ConditionAssessment(
            code=code,
            grade=grade,
            factor=code.condition_factor,
            badge=f"🔄 Вживаний ({grade:.1f}/10)",
            summary=summary,
            usage_counter=usage_counter,
        )
