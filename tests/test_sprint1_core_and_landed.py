from __future__ import annotations

import pytest

from omni_agent.core.compliance import ComplianceManager
from omni_agent.core.condition_grader import ConditionGrader
from omni_agent.core.landed_cost import LandedCostEngine
from omni_agent.core.models import (
    ChannelTrustTier,
    ConditionCode,
    Coupon,
    CustomsMode,
    DealTierBadge,
    NormalizedOffer,
    ShoppingTask,
    VerificationStatus,
)
from omni_agent.core.normalizer import (
    calculate_title_similarity,
    detect_kit_trap,
    extract_model_codes,
    is_accessory_or_part,
    normalize_title,
)


def test_models_enums_and_condition_factors() -> None:
    assert VerificationStatus.MANUAL_CHECK_LINK.value == "MANUAL_CHECK_LINK"
    assert DealTierBadge.VERIFIED_JACKPOT.value == "VERIFIED_JACKPOT"
    assert ChannelTrustTier.TIER_1_OFFICIAL.value == 1
    assert ConditionCode.BRAND_NEW.condition_factor == 1.0
    assert ConditionCode.OPEN_BOX.condition_factor == 0.93
    assert ConditionCode.REFURBISHED.condition_factor == 0.85
    assert ConditionCode.USED_GOOD.condition_factor == 0.72
    assert ConditionCode.FOR_PARTS.condition_factor == 0.30


def test_shopping_task_and_normalized_offer_contracts() -> None:
    task = ShoppingTask(
        task_id="task_001",
        raw_query="Dell U4323QE",
        canonical_mpns=["U4323QE"],
        estimated_weight_kg=18.5,
    )
    assert task.customs_mode == CustomsMode.STANDARD_150EUR
    assert task.allowed_conditions == [ConditionCode.BRAND_NEW, ConditionCode.OPEN_BOX]

    cond = ConditionGrader.grade("Dell U4323QE Новий")
    engine = LandedCostEngine()
    lc = engine.calculate(item_price_raw=38000.0, currency="UAH", route_country="UA")
    offer = NormalizedOffer(
        offer_id="off_1",
        task_id=task.task_id,
        channel="hotline",
        title="Монітор Dell UltraSharp U4323QE",
        url="https://hotline.ua/computer-monitory/dell-u4323qe/",
        price_raw=38000.0,
        currency="UAH",
        condition=cond,
        landed_cost=lc,
        raw_price_snippet="38 000 грн",
    )
    assert offer.verification_status == VerificationStatus.VERIFIED
    assert offer.landed_cost.realistic_landed_uah == 38000.0


def test_extract_model_codes_monitors_and_appliances() -> None:
    codes_lg = extract_model_codes('Монітор LG 43UN700-B 43" 4K UHD 60Hz IPS 2026')
    assert "43UN700-B" in codes_lg
    assert "4K" not in codes_lg
    assert "60HZ" not in codes_lg
    assert "2026" not in codes_lg

    codes_dell = extract_model_codes('Dell UltraSharp U4323QE 42.5" 4K 60Hz USB-C 90W')
    assert "U4323QE" in codes_dell
    assert "90W" not in codes_dell

    codes_giga = extract_model_codes('Gigabyte M32U 32" 144Hz 4K Gaming Monitor')
    assert "M32U" in codes_giga
    assert "144HZ" not in codes_giga

    codes_other = extract_model_codes("DeLonghi ECAM 22.110.B / Panasonic EY905D10 / 38WR85QC-W / DL24MP / C242 2400W 32GB")
    assert "ECAM22.110.B" in codes_other
    assert "EY905D10" in codes_other
    assert "38WR85QC-W" in codes_other
    assert "DL24MP" in codes_other
    assert "C242" in codes_other
    assert "2400W" not in codes_other
    assert "32GB" not in codes_other


def test_normalize_title_and_similarity() -> None:
    raw = "Навушники Sony WH-1000XM5 Black (WH1000XM5B.CE7) АКЦІЯ! Гарантія 12 міс"
    norm = normalize_title(raw)
    assert "АКЦІЯ" not in norm
    assert "Гарантія" not in norm
    assert "Sony WH-1000XM5 Black" == norm

    sim_same_mpn = calculate_title_similarity("Dell UltraSharp U4323QE", "Монітор Dell U4323QE Open Box")
    assert sim_same_mpn == 0.95

    sim_diff_mpn = calculate_title_similarity("Dell U4323QE", "Dell U3223QE")
    assert sim_diff_mpn < 0.30


def test_is_accessory_or_part() -> None:
    assert is_accessory_or_part("Кронштейн для монітора Dell U4323QE", "Dell U4323QE") is True
    assert is_accessory_or_part("Wall mount bracket for LG 43UN700-B", "LG 43UN700-B") is True
    assert is_accessory_or_part("Кабель USB-C 100W для ноутбука", "Dell U4323QE") is True
    assert is_accessory_or_part("Матриця для LG 43UN700-B", "LG 43UN700-B") is True
    assert is_accessory_or_part("Монітор Dell U4323QE 4K", "Dell U4323QE") is False
    # If the user explicitly searches for a mount, do not filter mounts out
    assert is_accessory_or_part("Кронштейн настільний NB F80", "Кронштейн NB F80") is False


def test_detect_kit_trap() -> None:
    assert detect_kit_trap("Makita DHP484Z Solo ohne Akku ohne Ladegerät") is not None
    assert "110V" in (detect_kit_trap("KitchenAid Artisan Mixer US Plug 110V only") or "")
    assert detect_kit_trap("Bosch GSR 18V-55 Body Only") is not None
    assert detect_kit_trap("Wkrętarka DeWalt DCD791 bez akumulatora") is not None
    assert detect_kit_trap("DeLonghi ECAM22.110.B 230V EU") is None


def test_condition_grader_for_parts_and_cracked_screen() -> None:
    res1 = ConditionGrader.grade("Монітор Dell U4323QE бита матриця, на запчастини")
    assert res1.code == ConditionCode.FOR_PARTS
    assert res1.factor == 0.30
    assert res1.grade <= 4.0

    res2 = ConditionGrader.grade("LG 43UN700-B смуги на матриці, defekt")
    assert res2.code == ConditionCode.FOR_PARTS

    res3 = ConditionGrader.grade("LG 43BN70U", description="Cracked screen, for parts only")
    assert res3.code == ConditionCode.FOR_PARTS


def test_condition_grader_open_box_refurb_new_used() -> None:
    ob = ConditionGrader.grade("DeLonghi ECAM22.110 Amazon Retourenkauf, пробіг 45 чашок")
    assert ob.code == ConditionCode.OPEN_BOX
    assert ob.factor == 0.93
    assert ob.usage_counter == 45

    ref = ConditionGrader.grade("Sony WH-1000XM5 Certified Refurbished після ТО")
    assert ref.code == ConditionCode.REFURBISHED
    assert ref.factor == 0.85

    new = ConditionGrader.grade("EcoFlow DELTA 2 Max Новий", raw_condition="Нове")
    assert new.code == ConditionCode.BRAND_NEW
    assert new.factor == 1.0

    used = ConditionGrader.grade("Кавомашина DeLonghi пробіг 4500 чашок", raw_condition="Б/в")
    assert used.code == ConditionCode.USED_GOOD
    assert used.factor == 0.72
    assert used.grade == 6.8


def test_compliance_gdpr_and_price_grounding() -> None:
    dirty = "Купити Dell U4323QE, дзвонити +38 (067) 123-45-67, test.user@example.com, вул. Хрещатик 22, кв. 5"
    clean = ComplianceManager.sanitize_gdpr_query(dirty)
    assert "example.com" not in clean
    assert "123-45-67" not in clean
    assert "Хрещатик" not in clean
    assert "Dell U4323QE" in clean

    ok, _ = ComplianceManager.verify_price_grounding(14999.0, "Ціна: 14\u00a0999 грн")
    assert ok is True

    ok_eu, _ = ComplianceManager.verify_price_grounding(649.99, "649,99 € incl. MwSt.")
    assert ok_eu is True

    bad, msg = ComplianceManager.verify_price_grounding(12000.0, "18 500 грн")
    assert bad is False
    assert "GROUNDING_MISMATCH" in msg


def test_landed_cost_amazon_de_direct_vs_meest_de_double_vat() -> None:
    engine = LandedCostEngine()

    # 800 EUR gross in DE, 5kg monitor
    direct = engine.calculate(
        item_price_raw=800.0,
        currency="EUR",
        route_country="DE",
        shipping_raw=35.0,
        weight_kg=5.0,
        direct_to_ua_export=True,
        customs_mode=CustomsMode.STANDARD_150EUR,
    )
    via_meest = engine.calculate(
        item_price_raw=800.0,
        currency="EUR",
        route_country="DE",
        shipping_raw=0.0,
        weight_kg=5.0,
        direct_to_ua_export=False,
        customs_mode=CustomsMode.STANDARD_150EUR,
    )

    # Direct export deducts 19% DE VAT: 800 * 45 = 36000; net = 36000 / 1.19 = 30252.10
    assert direct.vat_deducted_uah > 5700.0
    assert "DOUBLE_VAT_WARNING" not in direct.risk_flags

    # Forwarder does NOT deduct DE VAT and triggers DOUBLE_VAT_WARNING since >150 EUR
    assert via_meest.vat_deducted_uah == 0.0
    assert via_meest.forwarder_fee_uah > 0.0
    assert "DOUBLE_VAT_WARNING" in via_meest.risk_flags
    assert direct.realistic_landed_uah < via_meest.realistic_landed_uah


def test_landed_cost_energy_exempt_zero_duty() -> None:
    engine = LandedCostEngine()
    res = engine.calculate(
        item_price_raw=900.0,
        currency="EUR",
        route_country="DE",
        weight_kg=22.0,
        direct_to_ua_export=False,
        customs_mode=CustomsMode.ENERGY_EXEMPT,
    )
    assert res.customs_duty_vat_uah == 0.0
    assert res.broker_fee_uah == 0.0
    assert "DOUBLE_VAT_WARNING" not in res.risk_flags


def test_landed_cost_coupons_and_risk_flags() -> None:
    engine = LandedCostEngine()
    coupons = [
        Coupon(code="SAVE10", description="$10 off", discount_amount=10.0, min_spend=100.0, currency="USD"),
        Coupon(code="SAVE30", description="$30 off", discount_amount=30.0, min_spend=150.0, currency="USD"),
    ]
    # 160 USD -> eligible for SAVE30 -> 130 USD (~120.37 EUR < 150 EUR -> 0 customs!)
    res = engine.calculate(
        item_price_raw=160.0,
        currency="USD",
        route_country="US",
        weight_kg=1.0,
        coupons=coupons,
    )
    assert res.applied_coupon_code == "SAVE30"
    assert res.coupon_discount_raw == 30.0
    assert res.customs_duty_vat_uah == 0.0

    # Borderline 150 EUR FX risk (145 EUR)
    border = engine.calculate(item_price_raw=145.0, currency="EUR", route_country="PL", weight_kg=1.0)
    assert "BORDERLINE_150EUR_FX_RISK" in border.risk_flags

    # Dead zone unprofitable: cheap 15 EUR item with forwarder > 30% of item price and < 1000 UAH
    dead = engine.calculate(item_price_raw=15.0, currency="EUR", route_country="US", weight_kg=2.0)
    assert "DEAD_ZONE_UNPROFITABLE" in dead.risk_flags


def test_landed_cost_pln_fx_spread_and_ua_escrow() -> None:
    engine = LandedCostEngine()
    pl_res = engine.calculate(item_price_raw=500.0, currency="PLN", route_country="PL", weight_kg=1.0)
    # PLN uses 2% spread on realistic_landed_uah
    assert pl_res.fx_spread_uah > 0.0
    assert pl_res.realistic_landed_uah > pl_res.base_landed_uah

    ua_res = engine.calculate(
        item_price_raw=10000.0,
        currency="UAH",
        route_country="UA",
        local_escrow_shipping_uah=180.0,
    )
    assert ua_res.fx_spread_uah == 0.0
    assert ua_res.forwarder_fee_uah == 0.0
    assert ua_res.realistic_landed_uah == 10180.0


def test_calculate_foreign_breakeven_price_exact_inversion() -> None:
    engine = LandedCostEngine()
    target_ua_uah = 35000.0

    for route, cur, direct in [
        ("DE", "EUR", True),
        ("DE", "EUR", False),
        ("PL", "PLN", False),
        ("CZ", "CZK", False),
        ("US", "USD", False),
    ]:
        be = engine.calculate_foreign_breakeven_price(
            best_ua_uah=target_ua_uah,
            route_country=route,
            currency=cur,
            weight_kg=4.0,
            direct_to_ua_export=direct,
        )
        max_price = be["max_foreign_price"]
        assert max_price > 0.0
        assert be["currency"] == cur

        at_max = engine.calculate(
            item_price_raw=max_price,
            currency=cur,
            route_country=route,
            weight_kg=4.0,
            direct_to_ua_export=direct,
        )
        above_max = engine.calculate(
            item_price_raw=round(max_price + 0.05, 2),
            currency=cur,
            route_country=route,
            weight_kg=4.0,
            direct_to_ua_export=direct,
        )
        assert at_max.realistic_landed_uah <= target_ua_uah
        assert above_max.realistic_landed_uah > target_ua_uah


def test_calculate_foreign_breakeven_price_unprofitable_zero() -> None:
    engine = LandedCostEngine()
    be = engine.calculate_foreign_breakeven_price(
        best_ua_uah=100.0,
        route_country="US",
        currency="USD",
        weight_kg=5.0,
    )
    assert be["max_foreign_price"] == 0.0
