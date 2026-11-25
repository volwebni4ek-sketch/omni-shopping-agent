from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from omni_agent.core.condition_grader import ConditionGrader
from omni_agent.core.funnel import FunnelAuditReport, StatisticalFunnelEngine
from omni_agent.core.gemini_studio_opt import OptionalGeminiReviewer
from omni_agent.core.landed_cost import LandedCostEngine
from omni_agent.core.models import (
    ChannelTrustTier,
    ConditionCode,
    DealTierBadge,
    NormalizedOffer,
    ShoppingTask,
    VerificationStatus,
)
from omni_agent.core.normalizer import extract_model_codes
from omni_agent.core.pareto_topsis import ParetoTopsisEngine
from omni_agent.core.spec_scorer import DomainSpecScorer


def _make_offer(
    offer_id: str,
    title: str,
    price_uah: float,
    *,
    task_id: str = "task_mon",
    channel: str = "rozetka",
    route_country: str = "UA",
    trust_tier: ChannelTrustTier = ChannelTrustTier.TIER_1_OFFICIAL,
    in_stock: bool = True,
    delivery_days: int = 2,
    warranty_months: int = 36,
    raw_condition: str = "",
    specs: dict[str, Any] | None = None,
    seller_rating: float | None = 4.9,
    orders_or_reviews: int = 120,
    verification_status: VerificationStatus = VerificationStatus.VERIFIED,
) -> NormalizedOffer:
    cond = ConditionGrader.grade(title, raw_condition=raw_condition)
    lc = LandedCostEngine().calculate(
        item_price_raw=price_uah,
        currency="UAH",
        route_country="UA",
    )
    if route_country != "UA":
        # Preserve explicit realistic_landed_uah for deterministic unit tests across routes
        lc.route_country = route_country
        lc.realistic_landed_uah = round(price_uah, 2)
        lc.base_landed_uah = round(price_uah, 2)
    return NormalizedOffer(
        offer_id=offer_id,
        task_id=task_id,
        channel=channel,
        route_country=route_country,
        store_name=channel,
        trust_tier=trust_tier,
        title=title,
        mpn_codes=extract_model_codes(title),
        url=f"https://example.com/{offer_id}",
        price_raw=price_uah,
        currency="UAH",
        in_stock=in_stock,
        delivery_days=delivery_days,
        warranty_months=warranty_months,
        seller_rating=seller_rating,
        orders_or_reviews=orders_or_reviews,
        condition=cond,
        landed_cost=lc,
        verification_status=verification_status,
        specs=specs or {},
    )


def test_monitor_eye_care_scoring_ips_vs_oled_and_aspect() -> None:
    task = ShoppingTask(
        task_id="t_eye",
        raw_query='Великий монітор 43" 4K для роботи з текстом і кіно, eye care комфорт для очей',
        category="monitors_large_format",
    )
    dell_ips_black = _make_offer(
        "dell_u43",
        'Dell UltraSharp U4323QE 42.5" 4K UHD IPS Black ComfortView Plus Flicker-Free USB-C 90W KVM',
        42000.0,
        specs={"panel_type": "IPS Black", "comfortview_plus": True, "diagonal_inch": 42.5},
    )
    lg_ips_43 = _make_offer(
        "lg_43",
        'LG 43SQ700S 43" 4K UHD IPS Flicker-Safe USB-C Smart Remote',
        19500.0,
        specs={"panel_type": "IPS", "flicker_free": True, "diagonal_inch": 43.0},
    )
    oled_panel = _make_offer(
        "oled_42",
        'LG 42" 4K OLED Gaming Monitor 120Hz',
        46000.0,
        specs={"panel_type": "OLED", "diagonal_inch": 42.0},
    )
    cramped_34 = _make_offer(
        "uw_34",
        'Dell S3422DWG 34" 21:9 WQHD VA Ultrawide',
        18000.0,
        specs={"panel_type": "VA", "diagonal_inch": 34.0, "aspect_ratio": "21:9"},
    )

    DomainSpecScorer.score_offers([dell_ips_black, lg_ips_43, oled_panel, cramped_34], task)

    assert dell_ips_black.quality_score > lg_ips_43.quality_score
    assert lg_ips_43.quality_score > oled_panel.quality_score
    assert dell_ips_black.quality_score > oled_panel.quality_score
    assert lg_ips_43.quality_score > cramped_34.quality_score
    assert dell_ips_black.effective_quality_score == round(dell_ips_black.quality_score * 1.0, 1)


def test_power_station_and_general_scoring_profiles() -> None:
    ps_task = ShoppingTask(task_id="t_ps", raw_query="Зарядна станція LiFePO4 2kWh для квартири", category="power_stations")
    lfp_station = _make_offer(
        "ps_1",
        "EcoFlow DELTA 2 Max 2048Wh LiFePO4 3000 cycles 20ms UPS 30dB",
        55000.0,
        specs={"battery_chemistry": "LiFePO4", "capacity_wh": 2048, "cycles_count": 3000, "ups_ms": 20, "noise_db": 30},
    )
    nmc_station = _make_offer(
        "ps_2",
        "Generic Power Station 1000Wh NMC 800 cycles 35ms 58dB",
        30000.0,
        specs={"battery_chemistry": "NMC", "capacity_wh": 1000, "cycles_count": 800, "ups_ms": 35, "noise_db": 58},
    )
    DomainSpecScorer.score_offers([lfp_station, nmc_station], ps_task)
    assert lfp_station.quality_score > nmc_station.quality_score

    gen_task = ShoppingTask(task_id="t_gen", raw_query="Кавомашина DeLonghi ECAM22.110", category="appliances")
    tier_a = _make_offer(
        "gen_1",
        "Кавомашина DeLonghi ECAM22.110.B",
        16000.0,
        warranty_months=36,
        seller_rating=4.9,
        orders_or_reviews=250,
    )
    unknown_brand = _make_offer(
        "gen_2",
        "Кавоварка NoName X100",
        7000.0,
        warranty_months=0,
        seller_rating=3.2,
        orders_or_reviews=1,
    )
    DomainSpecScorer.score_offers([tier_a, unknown_brand], gen_task)
    assert tier_a.quality_score > unknown_brand.quality_score


def test_channel_score_tiers_warranty_urgency_and_stock() -> None:
    t1_fast = _make_offer("c1", "Dell U4323QE", 40000.0, trust_tier=ChannelTrustTier.TIER_1_OFFICIAL, delivery_days=2, warranty_months=36)
    t2_mid = _make_offer("c2", "Dell U4323QE", 35000.0, trust_tier=ChannelTrustTier.TIER_2_ESCROW, delivery_days=5, warranty_months=12)
    t3_slow = _make_offer("c3", "Dell U4323QE", 28000.0, trust_tier=ChannelTrustTier.TIER_3_UNVERIFIED, delivery_days=20, warranty_months=0)
    oos = _make_offer("c4", "Dell U4323QE", 39000.0, in_stock=False)

    s1 = DomainSpecScorer.compute_channel_score(t1_fast, urgency_days=10)
    s2 = DomainSpecScorer.compute_channel_score(t2_mid, urgency_days=10)
    s3 = DomainSpecScorer.compute_channel_score(t3_slow, urgency_days=10)
    s_oos = DomainSpecScorer.compute_channel_score(oos, urgency_days=10)

    assert s1 == 100.0
    assert 75.0 <= s2 <= 90.0
    assert s3 < 40.0
    assert s_oos == 10.0
    assert s1 > s2 > s3 > s_oos


def test_funnel_stage1_prunes_accessories_broken_screens_variant_and_kit_traps() -> None:
    task = ShoppingTask(
        task_id="t_s1",
        raw_query="Монітор Dell U4323QE",
        category="monitors_large_format",
    )
    valid = _make_offer("ok_1", 'Dell UltraSharp U4323QE 42.5" 4K IPS Black', 41000.0)
    cable = _make_offer("trap_cable", "Кабель USB-C 100W для Dell U4323QE", 650.0)
    mount = _make_offer("trap_mount", "Кронштейн для монітора Dell U4323QE", 2400.0)
    broken = _make_offer("trap_broken", "Монітор Dell U4323QE бита матриця на запчастини", 5000.0)
    ohne_akku = _make_offer("trap_akku", "Makita DHP484Z Solo ohne Akku", 4500.0)
    variant = _make_offer("trap_var", "Dell U4323QE (Підставка / чохол)", 3000.0, specs={"variant_trap": True})
    oos = _make_offer("trap_oos", "Dell U4323QE Новий", 40000.0, in_stock=False)

    report = StatisticalFunnelEngine.run(
        [valid, cable, mount, broken, ohne_akku, variant, oos],
        task,
    )
    assert isinstance(report, FunnelAuditReport)
    assert report.total_raw_offers == 7
    assert report.after_semantic_sieve == 1
    assert len(report.valid_candidates) == 1
    assert len(report.pruned_offers) == 6
    assert all(o.deal_badge == DealTierBadge.PRUNED_OUTLIER for o in report.pruned_offers)
    assert all(bool(o.prune_reason) for o in report.pruned_offers)


def test_funnel_stage1_allows_body_only_when_explicitly_requested() -> None:
    task = ShoppingTask(task_id="t_body", raw_query="Makita DHP484Z body only", category="tools")
    solo = _make_offer("solo_1", "Makita DHP484Z Body Only 18V", 4200.0)
    report = StatisticalFunnelEngine.run([solo], task)
    assert report.after_semantic_sieve == 1
    assert len(report.valid_candidates) == 1


def test_funnel_2d_jackpot_human_review_and_quarantine_matrix() -> None:
    task = ShoppingTask(
        task_id="t_2d",
        raw_query="LG 43SQ700S 43 4K IPS",
        category="monitors_large_format",
    )
    # 3 UA BRAND_NEW anchors around 20,000 UAH -> reference median = 20,000 UAH
    a1 = _make_offer("a1", "LG 43SQ700S 4K IPS", 20000.0, channel="hotline")
    a2 = _make_offer("a2", "LG 43SQ700S 4K IPS", 19800.0, channel="rozetka")
    a3 = _make_offer("a3", "LG 43SQ700S 4K IPS", 20200.0, channel="prom")

    # -38% discount in TIER_1_OFFICIAL -> 12,400 UAH -> VERIFIED_JACKPOT
    jackpot = _make_offer(
        "jp1",
        "LG 43SQ700S 4K IPS Amazon DE Direct",
        12400.0,
        channel="amazon_de",
        route_country="DE",
        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
        delivery_days=8,
    )
    # -40% discount in TIER_2_ESCROW -> 12,000 UAH -> HUMAN_REVIEW_DEAL
    human_rev = _make_offer(
        "hr1",
        "LG 43SQ700S 4K IPS Open Box OLX Доставка",
        12000.0,
        channel="olx",
        route_country="UA",
        trust_tier=ChannelTrustTier.TIER_2_ESCROW,
        raw_condition="Open Box",
        delivery_days=2,
    )
    # -65% discount in TIER_3_UNVERIFIED -> 7,000 UAH -> QUARANTINE_LOW_TRUST
    quarantine = _make_offer(
        "q1",
        "LG 43SQ700S 4K IPS Новий",
        7000.0,
        channel="telegram_unknown",
        route_country="UA",
        trust_tier=ChannelTrustTier.TIER_3_UNVERIFIED,
    )
    # < 25% of median even in Tier 1 -> suspicious price -> QUARANTINE_LOW_TRUST
    glitch = _make_offer(
        "q2",
        "LG 43SQ700S 4K IPS",
        3500.0,
        channel="ua_flash_shop",
        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
    )

    report = StatisticalFunnelEngine.run([a1, a2, a3, jackpot, human_rev, quarantine, glitch], task)

    assert report.reference_median_uah == 20000.0
    assert jackpot.deal_badge == DealTierBadge.VERIFIED_JACKPOT
    assert jackpot.discount_vs_median_pct == 38.0
    assert human_rev.deal_badge == DealTierBadge.HUMAN_REVIEW_DEAL
    assert human_rev.discount_vs_median_pct == 40.0
    assert quarantine.deal_badge == DealTierBadge.QUARANTINE_LOW_TRUST
    assert glitch.deal_badge == DealTierBadge.QUARANTINE_LOW_TRUST

    assert [o.offer_id for o in report.jackpots] == ["jp1"]
    assert [o.offer_id for o in report.human_review_deals] == ["hr1"]
    assert {o.offer_id for o in report.quarantine_offers} == {"q1", "q2"}
    assert "q1" not in {o.offer_id for o in report.valid_candidates}


def test_funnel_does_not_falsely_prune_legit_premium_dell_u4323qe() -> None:
    task = ShoppingTask(
        task_id="t_prem",
        raw_query='Монітор 43" 4K для роботи та кіно',
        category="monitors_large_format",
    )
    # 4 budget VA / standard monitors ~ 15,000-17,500 UAH
    b1 = _make_offer("b1", 'Samsung M7 43" 4K VA', 16000.0, channel="hotline", specs={"panel_type": "VA", "diagonal_inch": 43.0})
    b2 = _make_offer("b2", 'Samsung M7 43" 4K VA', 16500.0, channel="rozetka", specs={"panel_type": "VA", "diagonal_inch": 43.0})
    b3 = _make_offer("b3", 'LG 43SQ700S 43" 4K IPS', 18000.0, channel="prom", specs={"panel_type": "IPS", "diagonal_inch": 43.0})
    b4 = _make_offer("b4", 'Xiaomi 43" 4K VA', 15000.0, channel="hotline", specs={"panel_type": "VA", "diagonal_inch": 43.0})

    # Legitimate higher-tier Dell U4323QE IPS Black ComfortView Plus at 42,000 UAH (> Q3 + 1.5*IQR)
    dell = _make_offer(
        "dell_prem",
        'Dell UltraSharp U4323QE 42.5" 4K IPS Black ComfortView Plus Flicker-Free USB-C KVM',
        42000.0,
        channel="rozetka",
        specs={"panel_type": "IPS Black", "comfortview_plus": True, "diagonal_inch": 42.5},
    )
    # Genuine overpriced scalper of the exact same Samsung M7 at 38,000 UAH (> 1.8x median and worse/equal quality at higher price)
    scalper_m7 = _make_offer(
        "scalp_m7",
        'Samsung M7 43" 4K VA',
        38000.0,
        channel="prom",
        specs={"panel_type": "VA", "diagonal_inch": 43.0},
    )
    # Ensure MPN overlap for Samsung M7
    b1.mpn_codes = ["LS43BM700"]
    scalper_m7.mpn_codes = ["LS43BM700"]

    report = StatisticalFunnelEngine.run([b1, b2, b3, b4, dell, scalper_m7], task)

    valid_ids = {o.offer_id for o in report.valid_candidates}
    pruned_ids = {o.offer_id for o in report.pruned_offers}

    assert "dell_prem" in valid_ids
    assert "scalp_m7" in pruned_ids


def test_pareto_dominance_elimination_and_badge_upgrade() -> None:
    # Offer A dominates Offer B across all 3 criteria
    a = _make_offer("a", "LG 43UN700-B", 20000.0, trust_tier=ChannelTrustTier.TIER_1_OFFICIAL)
    a.effective_quality_score = 88.0
    a.channel_score = 95.0
    a.deal_badge = DealTierBadge.STANDARD_VALID

    b = _make_offer("b", "LG 43UN700-B", 22000.0, trust_tier=ChannelTrustTier.TIER_2_ESCROW)
    b.effective_quality_score = 80.0
    b.channel_score = 82.0
    b.deal_badge = DealTierBadge.STANDARD_VALID

    # Offer C is cheaper than A (non-dominated)
    c = _make_offer("c", "LG 43UN700-B Open Box", 14000.0, trust_tier=ChannelTrustTier.TIER_2_ESCROW, raw_condition="Open Box")
    c.effective_quality_score = 76.0
    c.channel_score = 80.0
    c.deal_badge = DealTierBadge.HUMAN_REVIEW_DEAL

    frontier = ParetoTopsisEngine.compute_pareto_frontier([a, b, c])
    frontier_ids = {o.offer_id for o in frontier}

    assert frontier_ids == {"a", "c"}
    assert a.is_pareto is True
    assert a.deal_badge == DealTierBadge.PARETO_OPTIMAL
    assert b.is_pareto is False
    assert c.is_pareto is True
    # HUMAN_REVIEW_DEAL badge is preserved when already special
    assert c.deal_badge == DealTierBadge.HUMAN_REVIEW_DEAL


def test_topsis_cost_vs_benefit_math() -> None:
    best = _make_offer("best", "Dell U4323QE", 28000.0)
    best.effective_quality_score = 92.0
    best.channel_score = 96.0

    mid = _make_offer("mid", "Dell U4323QE", 35000.0)
    mid.effective_quality_score = 85.0
    mid.channel_score = 88.0

    worst = _make_offer("worst", "Dell U4323QE", 46000.0)
    worst.effective_quality_score = 70.0
    worst.channel_score = 72.0

    ranked = ParetoTopsisEngine.rank_with_topsis([worst, mid, best])
    assert [o.offer_id for o in ranked] == ["best", "mid", "worst"]
    assert 0.0 <= worst.topsis_score < mid.topsis_score < best.topsis_score <= 1.0


def test_top_chart_quartet_and_hard_tradeoff_dilemmas() -> None:
    ua_official = _make_offer(
        "ua_off",
        'Dell UltraSharp U4323QE 42.5" 4K IPS Black',
        42000.0,
        channel="rozetka",
        route_country="UA",
        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
        delivery_days=1,
        warranty_months=36,
    )
    de_jackpot = _make_offer(
        "de_jp",
        'Dell UltraSharp U4323QE 42.5" 4K IPS Black Amazon DE',
        26000.0,
        channel="amazon_de",
        route_country="DE",
        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
        delivery_days=9,
        warranty_months=24,
    )
    de_jackpot.deal_badge = DealTierBadge.VERIFIED_JACKPOT
    de_jackpot.discount_vs_median_pct = 38.1

    olx_openbox = _make_offer(
        "olx_ob",
        "Dell UltraSharp U4323QE Open Box",
        29000.0,
        channel="olx",
        route_country="UA",
        trust_tier=ChannelTrustTier.TIER_2_ESCROW,
        raw_condition="Open Box",
        delivery_days=3,
        warranty_months=0,
    )
    olx_openbox.deal_badge = DealTierBadge.HUMAN_REVIEW_DEAL
    olx_openbox.discount_vs_median_pct = 31.0

    extra_ua = _make_offer(
        "ua_2",
        "Dell U4323QE",
        43500.0,
        channel="hotline",
        route_country="UA",
        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
        delivery_days=2,
        warranty_months=36,
    )

    task = ShoppingTask(task_id="t_q", raw_query="Dell U4323QE", category="monitors_large_format")
    DomainSpecScorer.score_offers([ua_official, de_jackpot, olx_openbox, extra_ua], task)
    ParetoTopsisEngine.compute_pareto_frontier([ua_official, de_jackpot, olx_openbox, extra_ua])
    ranked = ParetoTopsisEngine.rank_with_topsis([ua_official, de_jackpot, extra_ua])

    result = ParetoTopsisEngine.build_top_chart_quartet(
        ranked_offers=ranked,
        human_review_deals=[olx_openbox],
        reference_median_uah=42000.0,
    )

    quartet = result["quartet"]
    assert quartet["best_overall_topsis"] is not None
    assert quartet["best_buy_today_ua"] is not None
    assert quartet["smart_open_box_or_review"] is not None
    assert quartet["smart_open_box_or_review"].offer_id == "olx_ob"

    dilemmas = result["hard_tradeoff_dilemmas"]
    assert len(dilemmas) >= 2
    de_dilemma = next(d for d in dilemmas if d["challenger_offer_id"] == "de_jp")
    assert de_dilemma["savings_uah"] == 16000.0
    assert de_dilemma["savings_pct"] >= 35.0
    assert de_dilemma["extra_wait_days"] == 8
    assert de_dilemma["warranty_delta_months"] == -12
    assert "Економія 16000 ₴" in de_dilemma["dilemma_uk"]
    assert bool(de_jackpot.tradeoff_summary_uk)


@pytest.mark.asyncio
async def test_optional_gemini_reviewer_disabled_by_default_zero_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    reviewer = OptionalGeminiReviewer(api_key="", db_path=tmp_path / "cache.sqlite")
    assert reviewer.enabled is False

    res = await reviewer.review_shortlist(
        "task_free",
        {"candidates": [{"title": "Dell U4323QE", "realistic_landed_uah": 38000}]},
    )
    assert res["source"] == "deterministic_local"
    assert res["reason"] == "GEMINI_API_KEY_NOT_SET"
    assert "Dell U4323QE" in res["summary_uk"]


@pytest.mark.asyncio
async def test_optional_gemini_reviewer_sqlite_cache_and_max_calls_cap(tmp_path: Path) -> None:
    db_file = tmp_path / "gemini_cache.sqlite"
    call_counter = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_counter["count"] += 1
        body = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": f"AI аудит #{call_counter['count']}: оптимальний Dell U4323QE з Amazon DE."}
                        ]
                    }
                }
            ]
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    reviewer = OptionalGeminiReviewer(
        api_key="test-fake-key",
        db_path=db_file,
        max_calls_per_task=5,
    )
    assert reviewer.enabled is True

    payload = {"candidates": [{"title": "Dell U4323QE", "realistic_landed_uah": 26000}]}
    first = await reviewer.review_shortlist("task_cap", payload, transport=transport)
    assert first["source"] == "gemini-2.5-flash"
    assert first["cache_hit"] is False
    assert call_counter["count"] == 1

    # Second call with identical payload must hit SQLite cache (no extra HTTP call)
    second = await reviewer.review_shortlist("task_cap", payload, transport=transport)
    assert second["source"] == "gemini-2.5-flash"
    assert second["cache_hit"] is True
    assert call_counter["count"] == 1

    # Verify SQLite table `llm_eval_cache` has the stored row
    with sqlite3.connect(db_file) as conn:
        rows = conn.execute("SELECT model, task_id, response_json FROM llm_eval_cache").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "gemini-2.5-flash"
        assert rows[0][1] == "task_cap"
        assert "Dell U4323QE" in json.loads(rows[0][2])["summary_uk"]

    # Exhaust remaining 4 calls on unique payloads -> 6th distinct call hits hard cap max_calls_per_task=5
    for idx in range(2, 6):
        res = await reviewer.review_shortlist("task_cap", {"candidates": [{"n": idx}]}, transport=transport)
        assert res["source"] == "gemini-2.5-flash"
    assert call_counter["count"] == 5

    capped = await reviewer.review_shortlist("task_cap", {"candidates": [{"n": 99}]}, transport=transport)
    assert capped["source"] == "deterministic_local"
    assert "MAX_CALLS_REACHED" in capped["reason"]
    assert call_counter["count"] == 5
