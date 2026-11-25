from __future__ import annotations

import statistics
from typing import Optional

from pydantic import BaseModel, Field

from omni_agent.core.models import (
    ChannelTrustTier,
    ConditionCode,
    DealTierBadge,
    NormalizedOffer,
    ShoppingTask,
    VerificationStatus,
)
from omni_agent.core.normalizer import detect_kit_trap, is_accessory_or_part
from omni_agent.core.spec_scorer import DomainSpecScorer


class FunnelAuditReport(BaseModel):
    task_id: str
    total_raw_offers: int
    after_semantic_sieve: int
    reference_median_uah: float
    iqr_q1_uah: float
    iqr_q3_uah: float
    iqr_uah: float
    valid_candidates: list[NormalizedOffer] = Field(default_factory=list)
    jackpots: list[NormalizedOffer] = Field(default_factory=list)
    human_review_deals: list[NormalizedOffer] = Field(default_factory=list)
    quarantine_offers: list[NormalizedOffer] = Field(default_factory=list)
    pruned_offers: list[NormalizedOffer] = Field(default_factory=list)
    pruned_summary_uk: str = ""


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile (0.0 .. 1.0) on a pre-sorted non-empty list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f))


def _task_wants_body_only(task: ShoppingTask) -> bool:
    text = f"{task.raw_query} {task.human_notes}".lower()
    return any(
        kw in text
        for kw in ("body only", "solo", "ohne akku", "bez akumulatora", "без акумулятора", "тушка")
    )


class StatisticalFunnelEngine:
    """
    3-Stage Statistical Elimination Funnel:
      Stage 1: Semantic, Condition & Trap Sieve
      Stage 2: Robust Median & IQR Outlier Pruning (with quality-tier protection)
      Stage 3: 2D Anomaly & Jackpot Matrix (Discount % x Trust Tier)
    """

    UA_ANCHOR_CHANNELS = {"hotline", "rozetka", "prom"}

    @staticmethod
    def _is_ua_brand_new_anchor(offer: NormalizedOffer) -> bool:
        ch_low = (offer.channel or "").lower()
        return (
            offer.route_country.upper() == "UA"
            and offer.condition.code == ConditionCode.BRAND_NEW
            and any(anc in ch_low for anc in StatisticalFunnelEngine.UA_ANCHOR_CHANNELS)
        )

    @staticmethod
    def _shares_exact_mpn(a: NormalizedOffer, b: NormalizedOffer) -> bool:
        if not a.mpn_codes or not b.mpn_codes:
            return False
        set_a = {c.upper().strip() for c in a.mpn_codes if c}
        set_b = {c.upper().strip() for c in b.mpn_codes if c}
        return bool(set_a & set_b)

    @classmethod
    def run(
        cls,
        offers: list[NormalizedOffer],
        task: ShoppingTask,
        rescore_specs: bool = True,
    ) -> FunnelAuditReport:
        total_raw = len(offers)
        pruned: list[NormalizedOffer] = []
        stage1_survivors: list[NormalizedOffer] = []

        wants_body_only = _task_wants_body_only(task)
        allowed_conds = set(task.allowed_conditions)

        # ---- Stage 1: Semantic, Condition & Trap Sieve ----
        for offer in offers:
            if offer.verification_status != VerificationStatus.VERIFIED:
                offer.deal_badge = DealTierBadge.PRUNED_OUTLIER
                offer.prune_reason = f"Статус верифікації: {offer.verification_status.value}"
                pruned.append(offer)
                continue

            if not offer.in_stock or offer.landed_cost.realistic_landed_uah <= 0:
                offer.deal_badge = DealTierBadge.PRUNED_OUTLIER
                offer.prune_reason = "Немає в наявності або некоректна ціна"
                pruned.append(offer)
                continue

            if is_accessory_or_part(offer.title, task.raw_query):
                offer.deal_badge = DealTierBadge.PRUNED_OUTLIER
                offer.prune_reason = "Аксесуар або запчастина (кабель, кронштейн, матриця тощо)"
                pruned.append(offer)
                continue

            if offer.specs.get("variant_trap") is True:
                offer.deal_badge = DealTierBadge.PRUNED_OUTLIER
                offer.prune_reason = "Пастка мульти-SKU варіанту (variant_trap)"
                pruned.append(offer)
                continue

            kit_trap_reason = detect_kit_trap(offer.title)
            if kit_trap_reason and not (wants_body_only and "110v" not in kit_trap_reason.lower() and "plug" not in kit_trap_reason.lower()):
                offer.deal_badge = DealTierBadge.PRUNED_OUTLIER
                offer.prune_reason = kit_trap_reason
                pruned.append(offer)
                continue

            if offer.condition.code not in allowed_conds:
                offer.deal_badge = DealTierBadge.PRUNED_OUTLIER
                offer.prune_reason = f"Недопустимий стан товару: {offer.condition.code.value}"
                pruned.append(offer)
                continue

            if rescore_specs:
                DomainSpecScorer.score_offer(offer, task)

            stage1_survivors.append(offer)

        after_sieve = len(stage1_survivors)

        if not stage1_survivors:
            return FunnelAuditReport(
                task_id=task.task_id,
                total_raw_offers=total_raw,
                after_semantic_sieve=0,
                reference_median_uah=0.0,
                iqr_q1_uah=0.0,
                iqr_q3_uah=0.0,
                iqr_uah=0.0,
                pruned_offers=pruned,
                pruned_summary_uk=f"Відсіяно {len(pruned)} пропозицій на етапі семантичного фільтру (0 валідних кандидатів).",
            )

        # ---- Stage 2: Robust Median & IQR ----
        ua_new_anchors = [
            o.landed_cost.realistic_landed_uah
            for o in stage1_survivors
            if cls._is_ua_brand_new_anchor(o)
        ]
        all_brand_new = [
            o.landed_cost.realistic_landed_uah
            for o in stage1_survivors
            if o.condition.code == ConditionCode.BRAND_NEW
        ]
        all_prices = [o.landed_cost.realistic_landed_uah for o in stage1_survivors]

        if len(ua_new_anchors) >= 2:
            anchor_prices = ua_new_anchors
        elif all_brand_new:
            anchor_prices = all_brand_new
        else:
            anchor_prices = all_prices

        reference_median = round(float(statistics.median(anchor_prices)), 2)

        sorted_all = sorted(all_prices)
        q1 = round(_percentile(sorted_all, 0.25), 2)
        q3 = round(_percentile(sorted_all, 0.75), 2)
        iqr = round(max(q3 - q1, reference_median * 0.10), 2)
        upper_fence = q3 + 1.5 * iqr

        # Determine top 10% effective quality threshold for premium tier protection
        sorted_eff_q = sorted(o.effective_quality_score for o in stage1_survivors)
        top_10_quality_cutoff = _percentile(sorted_eff_q, 0.90)

        stage2_survivors: list[NormalizedOffer] = []
        for offer in stage1_survivors:
            price = offer.landed_cost.realistic_landed_uah
            if reference_median > 0:
                offer.discount_vs_median_pct = round(
                    ((reference_median - price) / reference_median) * 100.0, 1
                )
            else:
                offer.discount_vs_median_pct = 0.0

            # Overpriced outlier check with quality-tier protection
            is_overpriced = False
            overprice_reason = ""

            if price > upper_fence:
                has_equal_or_better_cheaper = any(
                    other is not offer
                    and other.effective_quality_score >= offer.effective_quality_score
                    and other.landed_cost.realistic_landed_uah < 0.80 * price
                    for other in stage1_survivors
                )
                is_same_mpn_scalper = any(
                    other is not offer
                    and cls._shares_exact_mpn(offer, other)
                    and price > 1.8 * reference_median
                    for other in stage1_survivors
                ) or (bool(offer.mpn_codes) and price > 1.8 * reference_median)

                if offer.effective_quality_score >= top_10_quality_cutoff and not has_equal_or_better_cheaper and not is_same_mpn_scalper:
                    is_overpriced = False
                elif has_equal_or_better_cheaper or is_same_mpn_scalper:
                    is_overpriced = True
                    overprice_reason = (
                        f"Завищена ціна ({round(price)} ₴ > межі IQR {round(upper_fence)} ₴ "
                        "за наявності рівноцінної/кращої дешевшої альтернативи)"
                    )

            elif reference_median > 0 and price > 1.8 * reference_median:
                # Exact same MPN priced > 1.8x median
                has_same_mpn_peer = any(
                    other is not offer and cls._shares_exact_mpn(offer, other)
                    for other in stage1_survivors
                )
                if has_same_mpn_peer:
                    is_overpriced = True
                    overprice_reason = (
                        f"Спекулятивна націнка для того ж MPN ({round(price)} ₴ > 1.8× медіани {round(reference_median)} ₴)"
                    )

            if is_overpriced:
                offer.deal_badge = DealTierBadge.PRUNED_OUTLIER
                offer.prune_reason = overprice_reason
                pruned.append(offer)
            else:
                stage2_survivors.append(offer)

        # ---- Stage 3: 2D Anomaly & Jackpot Matrix (Discount % x Trust Tier) ----
        valid_candidates: list[NormalizedOffer] = []
        jackpots: list[NormalizedOffer] = []
        human_review_deals: list[NormalizedOffer] = []
        quarantine_offers: list[NormalizedOffer] = []

        for offer in stage2_survivors:
            price = offer.landed_cost.realistic_landed_uah
            disc = offer.discount_vs_median_pct
            suspicious_low = reference_median > 0 and price < 0.25 * reference_median

            if suspicious_low:
                offer.deal_badge = DealTierBadge.QUARANTINE_LOW_TRUST
                offer.prune_reason = (
                    f"Підозріло низька ціна ({round(price)} ₴ < 25% від медіани {round(reference_median)} ₴)"
                )
                quarantine_offers.append(offer)
                continue

            if disc >= 30.0:
                if offer.trust_tier == ChannelTrustTier.TIER_1_OFFICIAL:
                    offer.deal_badge = DealTierBadge.VERIFIED_JACKPOT
                    jackpots.append(offer)
                    valid_candidates.append(offer)
                elif offer.trust_tier == ChannelTrustTier.TIER_2_ESCROW:
                    offer.deal_badge = DealTierBadge.HUMAN_REVIEW_DEAL
                    human_review_deals.append(offer)
                    valid_candidates.append(offer)
                else:
                    offer.deal_badge = DealTierBadge.QUARANTINE_LOW_TRUST
                    offer.prune_reason = (
                        f"Знижка {disc:.1f}% у неперевіреному каналі (Tier 3) — потрібен ручний аудит"
                    )
                    quarantine_offers.append(offer)
            else:
                if offer.trust_tier == ChannelTrustTier.TIER_3_UNVERIFIED:
                    offer.deal_badge = DealTierBadge.QUARANTINE_LOW_TRUST
                    offer.prune_reason = "Неперевірений продавець (Tier 3) виключено з авто-переможців"
                    quarantine_offers.append(offer)
                else:
                    offer.deal_badge = DealTierBadge.STANDARD_VALID
                    valid_candidates.append(offer)

        summary_uk = (
            f"Оброблено {total_raw} пропозицій → після семантичного сита: {after_sieve} → "
            f"валідних кандидатів: {len(valid_candidates)} "
            f"(Джекпоти: {len(jackpots)}, На перевірку: {len(human_review_deals)}, "
            f"Карантин: {len(quarantine_offers)}, Відсіяно: {len(pruned)}). "
            f"Опорна медіана: {round(reference_median)} ₴ (IQR: {round(iqr)} ₴)."
        )

        return FunnelAuditReport(
            task_id=task.task_id,
            total_raw_offers=total_raw,
            after_semantic_sieve=after_sieve,
            reference_median_uah=reference_median,
            iqr_q1_uah=q1,
            iqr_q3_uah=q3,
            iqr_uah=iqr,
            valid_candidates=valid_candidates,
            jackpots=jackpots,
            human_review_deals=human_review_deals,
            quarantine_offers=quarantine_offers,
            pruned_offers=pruned,
            pruned_summary_uk=summary_uk,
        )
