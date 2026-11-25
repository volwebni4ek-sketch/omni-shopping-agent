from __future__ import annotations

import math
from typing import Any, Optional

from omni_agent.core.models import (
    ChannelTrustTier,
    ConditionCode,
    DealTierBadge,
    NormalizedOffer,
)


class ParetoTopsisEngine:
    """
    Multi-Criteria Decision Analysis Engine:
      1. Pareto Frontier computation (maximize effective_quality_score & channel_score,
         minimize realistic_landed_uah)
      2. Vector-normalized TOPSIS ranking with cost + benefit criteria
      3. Top-Chart Quartet builder & Hard Trade-Off dilemma generator
    """

    @staticmethod
    def compute_pareto_frontier(offers: list[NormalizedOffer]) -> list[NormalizedOffer]:
        """
        Marks offer.is_pareto = True for non-dominated offers.
        If offer.deal_badge == STANDARD_VALID and offer.is_pareto, upgrades to PARETO_OPTIMAL.
        Returns the list of Pareto-optimal offers.
        """
        frontier: list[NormalizedOffer] = []
        for i, cand in enumerate(offers):
            dominated = False
            for j, other in enumerate(offers):
                if i == j:
                    continue
                q_ok = other.effective_quality_score >= cand.effective_quality_score
                c_ok = other.channel_score >= cand.channel_score
                p_ok = other.landed_cost.realistic_landed_uah <= cand.landed_cost.realistic_landed_uah

                strictly_better = (
                    other.effective_quality_score > cand.effective_quality_score
                    or other.channel_score > cand.channel_score
                    or other.landed_cost.realistic_landed_uah < cand.landed_cost.realistic_landed_uah
                )
                if q_ok and c_ok and p_ok and strictly_better:
                    dominated = True
                    break

            cand.is_pareto = not dominated
            if cand.is_pareto:
                if cand.deal_badge == DealTierBadge.STANDARD_VALID:
                    cand.deal_badge = DealTierBadge.PARETO_OPTIMAL
                frontier.append(cand)
            else:
                if cand.deal_badge == DealTierBadge.PARETO_OPTIMAL:
                    cand.deal_badge = DealTierBadge.STANDARD_VALID

        return frontier

    @staticmethod
    def rank_with_topsis(
        offers: list[NormalizedOffer],
        price_weight: float = 0.35,
        quality_weight: float = 0.40,
        channel_weight: float = 0.25,
    ) -> list[NormalizedOffer]:
        """
        Vector-normalized TOPSIS where:
          - effective_quality_score: benefit (maximize)
          - channel_score: benefit (maximize)
          - realistic_landed_uah: cost (minimize)
        Populates offer.topsis_score in [0.0, 1.0] and returns sorted descending.
        """
        if not offers:
            return []

        if len(offers) == 1:
            only = offers[0]
            only.topsis_score = round(
                min(
                    1.0,
                    max(
                        0.0,
                        (
                            quality_weight * (only.effective_quality_score / 100.0)
                            + channel_weight * (only.channel_score / 100.0)
                            + price_weight * 0.85
                        )
                        / max(1e-9, price_weight + quality_weight + channel_weight),
                    ),
                ),
                4,
            )
            return [only]

        total_w = price_weight + quality_weight + channel_weight
        if total_w <= 0:
            w_p, w_q, w_c = 0.35, 0.40, 0.25
        else:
            w_p = price_weight / total_w
            w_q = quality_weight / total_w
            w_c = channel_weight / total_w

        prices = [max(1.0, float(o.landed_cost.realistic_landed_uah)) for o in offers]
        quals = [max(0.0, float(o.effective_quality_score)) for o in offers]
        chans = [max(0.0, float(o.channel_score)) for o in offers]

        denom_p = math.sqrt(sum(v * v for v in prices)) or 1.0
        denom_q = math.sqrt(sum(v * v for v in quals)) or 1.0
        denom_c = math.sqrt(sum(v * v for v in chans)) or 1.0

        v_p = [(v / denom_p) * w_p for v in prices]
        v_q = [(v / denom_q) * w_q for v in quals]
        v_c = [(v / denom_c) * w_c for v in chans]

        # Ideal best (A+): min cost for price, max benefit for quality & channel
        best_p, worst_p = min(v_p), max(v_p)
        best_q, worst_q = max(v_q), min(v_q)
        best_c, worst_c = max(v_c), min(v_c)

        for idx, offer in enumerate(offers):
            d_best = math.sqrt(
                (v_p[idx] - best_p) ** 2
                + (v_q[idx] - best_q) ** 2
                + (v_c[idx] - best_c) ** 2
            )
            d_worst = math.sqrt(
                (v_p[idx] - worst_p) ** 2
                + (v_q[idx] - worst_q) ** 2
                + (v_c[idx] - worst_c) ** 2
            )
            if (d_best + d_worst) == 0:
                score = 0.5
            else:
                score = d_worst / (d_best + d_worst)
            offer.topsis_score = round(score, 4)

        return sorted(offers, key=lambda o: (o.topsis_score, o.is_pareto, -o.landed_cost.realistic_landed_uah), reverse=True)

    @staticmethod
    def _build_tradeoff_summary(offer: NormalizedOffer, reference_median_uah: float) -> str:
        price = round(offer.landed_cost.realistic_landed_uah)
        parts = [
            f"Якість {offer.effective_quality_score:.1f}/100 ({offer.condition.code.value})",
            f"Канал {offer.channel_score:.0f}/100 ({offer.channel}, {offer.delivery_days} дн.)",
            f"Landed {price} ₴",
        ]
        if offer.discount_vs_median_pct >= 5.0 and reference_median_uah > 0:
            parts.append(f"вигода -{offer.discount_vs_median_pct:.1f}% від медіани")
        elif offer.discount_vs_median_pct <= -5.0 and reference_median_uah > 0:
            parts.append(f"+{abs(offer.discount_vs_median_pct):.1f}% до медіани")
        if offer.warranty_months > 0:
            parts.append(f"гарантія {offer.warranty_months} міс.")
        else:
            parts.append("без офіційної гарантії")
        return " · ".join(parts)

    @classmethod
    def build_top_chart_quartet(
        cls,
        ranked_offers: list[NormalizedOffer],
        human_review_deals: list[NormalizedOffer],
        reference_median_uah: float,
    ) -> dict[str, Any]:
        """
        Selects up to 4 distinct hero slots:
          - best_overall_topsis
          - verified_jackpot_or_import
          - best_buy_today_ua
          - smart_open_box_or_review
        Also builds hard_tradeoff_dilemmas whenever an import or Open-Box offer saves >= 15%
        vs the best local UA official offer.
        """
        all_pool = list(ranked_offers)
        for hr in human_review_deals:
            if all(o.offer_id != hr.offer_id for o in all_pool):
                all_pool.append(hr)

        for offer in all_pool:
            offer.tradeoff_summary_uk = cls._build_tradeoff_summary(offer, reference_median_uah)

        used_ids: set[str] = set()
        hero_slots: dict[str, Optional[NormalizedOffer]] = {
            "best_overall_topsis": None,
            "verified_jackpot_or_import": None,
            "best_buy_today_ua": None,
            "smart_open_box_or_review": None,
        }

        # 1. best_overall_topsis
        if ranked_offers:
            hero = ranked_offers[0]
            hero_slots["best_overall_topsis"] = hero
            used_ids.add(hero.offer_id)

        # 2. verified_jackpot_or_import
        jackpot_or_import = [
            o
            for o in ranked_offers
            if o.offer_id not in used_ids
            and (o.deal_badge == DealTierBadge.VERIFIED_JACKPOT or o.route_country.upper() != "UA")
        ]
        if jackpot_or_import:
            jackpot_or_import.sort(
                key=lambda o: (
                    o.deal_badge == DealTierBadge.VERIFIED_JACKPOT,
                    o.discount_vs_median_pct,
                    o.topsis_score,
                ),
                reverse=True,
            )
            pick = jackpot_or_import[0]
            hero_slots["verified_jackpot_or_import"] = pick
            used_ids.add(pick.offer_id)

        # 3. best_buy_today_ua (in-stock local UA official/tier1-2 fast delivery)
        ua_today_cands = [
            o
            for o in ranked_offers
            if o.offer_id not in used_ids
            and o.route_country.upper() == "UA"
            and o.condition.code == ConditionCode.BRAND_NEW
            and o.delivery_days <= 4
        ]
        if not ua_today_cands:
            ua_today_cands = [
                o
                for o in ranked_offers
                if o.offer_id not in used_ids and o.route_country.upper() == "UA"
            ]
        if ua_today_cands:
            ua_today_cands.sort(
                key=lambda o: (
                    o.trust_tier == ChannelTrustTier.TIER_1_OFFICIAL,
                    o.topsis_score,
                    -o.landed_cost.realistic_landed_uah,
                ),
                reverse=True,
            )
            pick = ua_today_cands[0]
            hero_slots["best_buy_today_ua"] = pick
            used_ids.add(pick.offer_id)

        # 4. smart_open_box_or_review
        review_or_openbox = [
            o
            for o in (list(human_review_deals) + list(ranked_offers))
            if o.offer_id not in used_ids
            and (
                o.deal_badge == DealTierBadge.HUMAN_REVIEW_DEAL
                or o.condition.code in (ConditionCode.OPEN_BOX, ConditionCode.REFURBISHED)
            )
        ]
        if review_or_openbox:
            review_or_openbox.sort(
                key=lambda o: (
                    o.deal_badge == DealTierBadge.HUMAN_REVIEW_DEAL,
                    o.discount_vs_median_pct,
                    o.topsis_score,
                ),
                reverse=True,
            )
            pick = review_or_openbox[0]
            hero_slots["smart_open_box_or_review"] = pick
            used_ids.add(pick.offer_id)

        # Build Hard Trade-Off Dilemmas: import or Open-Box saving >= 15% vs best local UA official offer
        ua_official_offers = [
            o
            for o in ranked_offers
            if o.route_country.upper() == "UA"
            and o.trust_tier == ChannelTrustTier.TIER_1_OFFICIAL
            and o.condition.code == ConditionCode.BRAND_NEW
        ]
        hard_tradeoff_dilemmas: list[dict[str, Any]] = []

        if ua_official_offers:
            best_ua = min(ua_official_offers, key=lambda o: o.landed_cost.realistic_landed_uah)
            ua_price = best_ua.landed_cost.realistic_landed_uah

            for alt in all_pool:
                if alt.offer_id == best_ua.offer_id:
                    continue
                is_import_or_openbox = (
                    alt.route_country.upper() != "UA"
                    or alt.condition.code in (ConditionCode.OPEN_BOX, ConditionCode.REFURBISHED)
                )
                if not is_import_or_openbox or ua_price <= 0:
                    continue

                alt_price = alt.landed_cost.realistic_landed_uah
                savings_uah = round(ua_price - alt_price, 2)
                savings_pct = round((savings_uah / ua_price) * 100.0, 1)

                if savings_pct >= 15.0:
                    extra_wait_days = max(0, alt.delivery_days - best_ua.delivery_days)
                    warranty_delta_months = alt.warranty_months - best_ua.warranty_months
                    hard_tradeoff_dilemmas.append(
                        {
                            "baseline_ua_offer_id": best_ua.offer_id,
                            "baseline_ua_title": best_ua.title,
                            "baseline_ua_price_uah": round(ua_price, 2),
                            "challenger_offer_id": alt.offer_id,
                            "challenger_title": alt.title,
                            "challenger_channel": alt.channel,
                            "challenger_route": alt.route_country,
                            "challenger_condition": alt.condition.code.value,
                            "challenger_price_uah": round(alt_price, 2),
                            "savings_uah": savings_uah,
                            "savings_pct": savings_pct,
                            "extra_wait_days": extra_wait_days,
                            "warranty_delta_months": warranty_delta_months,
                            "dilemma_uk": (
                                f"Економія {round(savings_uah)} ₴ (-{savings_pct:.1f}%) на {alt.channel} "
                                f"({alt.route_country}, {alt.condition.code.value}) vs офіційний UA ({best_ua.channel}): "
                                f"очікування +{extra_wait_days} дн., гарантія {alt.warranty_months} міс. vs {best_ua.warranty_months} міс."
                            ),
                        }
                    )

        hard_tradeoff_dilemmas.sort(key=lambda d: d["savings_uah"], reverse=True)

        return {
            "quartet": hero_slots,
            "hard_tradeoff_dilemmas": hard_tradeoff_dilemmas,
            "reference_median_uah": reference_median_uah,
        }
