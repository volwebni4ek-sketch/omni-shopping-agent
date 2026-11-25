from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omni_agent.connectors.olx import OLXConnector
from omni_agent.connectors.rozetka_smart import RozetkaConnector
from omni_agent.core.condition_grader import ConditionGrader
from omni_agent.core.funnel import StatisticalFunnelEngine
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
from omni_agent.core.normalizer import (
    extract_model_codes,
    extract_ram_gb,
    is_direct_product_url,
)
from omni_agent.core.spec_scorer import DomainSpecScorer
from omni_agent.orchestrator import OmniOrchestrator


def _make_offer(
    offer_id: str,
    title: str,
    price_uah: float,
    *,
    channel: str = "rozetka",
    url: str = "https://rozetka.com.ua/ua/apple-mac-studio/p579054886/",
    store_name: str = "Rozetka.ua",
    trust_tier: ChannelTrustTier = ChannelTrustTier.TIER_1_OFFICIAL,
    raw_condition: str = "",
    specs: dict[str, Any] | None = None,
) -> NormalizedOffer:
    cond = ConditionGrader.grade(title, raw_condition=raw_condition)
    lc = LandedCostEngine().calculate(
        item_price_raw=price_uah,
        currency="UAH",
        route_country="UA",
    )
    return NormalizedOffer(
        offer_id=offer_id,
        task_id="t_sprint6",
        channel=channel,
        route_country="UA",
        store_name=store_name,
        trust_tier=trust_tier,
        title=title,
        mpn_codes=extract_model_codes(title),
        url=url,
        price_raw=price_uah,
        currency="UAH",
        in_stock=True,
        delivery_days=1,
        warranty_months=12,
        condition=cond,
        landed_cost=lc,
        verification_status=VerificationStatus.VERIFIED,
        raw_price_snippet=f"{price_uah:.0f} ₴",
        is_grounded=True,
        specs=specs or {},
    )


def test_is_direct_product_url_across_channels() -> None:
    # Empty / generic search rejections
    assert is_direct_product_url("", "rozetka") is False
    assert is_direct_product_url("https://rozetka.com.ua/ua/search/?text=mac+studio", "rozetka") is False
    assert is_direct_product_url("https://rozetka.com.ua/ua/monitors/c80089/", "rozetka") is False
    assert is_direct_product_url("https://rozetka.com.ua/ua/apple_mac_studio/p579054886/", "rozetka") is True

    # OLX
    assert is_direct_product_url("https://www.olx.ua/d/uk/obyavlenie/mac-studio-128gb-IDXyZ99.html", "olx") is True
    assert is_direct_product_url("https://www.olx.ua/uk/list/q-mac-studio/", "olx") is False
    assert is_direct_product_url("https://www.olx.ua/monitory/q-lg-43/", "olx") is False
    assert is_direct_product_url("https://www.olx.ua/kiev/q-lg-43/", "olx") is False

    # Prom
    assert is_direct_product_url("https://prom.ua/ua/p9994301-lg-43sq700s-openbox.html", "prom") is True
    assert is_direct_product_url("https://prom.ua/ua/search?search_term=mac+studio", "prom") is False
    assert is_direct_product_url("https://prom.ua/ua/Monitory", "prom") is False

    # Hotline
    assert is_direct_product_url("https://hotline.ua/ua/computer-monitory/samsung-smart-m7-s43fm700uu/", "hotline") is True
    assert is_direct_product_url("https://hotline.ua/ua/sr/?q=mac+studio", "hotline") is False
    assert is_direct_product_url("https://hotline.ua/ua/monitory/20352290/", "hotline") is False

    # Amazon EU & US
    assert is_direct_product_url("https://www.amazon.de/dp/B0BSHF7WHW", "amazon_de") is True
    assert is_direct_product_url("https://www.amazon.de/gp/product/B0BSHF7WHW", "amazon_de") is True
    assert is_direct_product_url("https://www.amazon.de/s?k=mac+studio", "amazon_de") is False

    # AliExpress
    assert is_direct_product_url("https://www.aliexpress.com/item/100500111.html", "aliexpress") is True
    assert is_direct_product_url("https://www.aliexpress.com/w/wholesale-mac-studio.html", "aliexpress") is False

    # Other / unknown channels
    assert is_direct_product_url("https://www.idealo.de/preisvergleich/OffersOfProduct/2023.html", "idealo_de") is True
    assert is_direct_product_url("https://www.ceneo.pl/154321", "ceneo_pl") is True
    assert is_direct_product_url("https://www.ceneo.pl/search?q=lg", "ceneo_pl") is False


def test_extract_ram_gb_helper() -> None:
    assert extract_ram_gb("Apple Mac Studio M2 Ultra 128GB / 2TB SSD") == 128
    assert extract_ram_gb("Framework Desktop Ryzen AI Max+ 395 128 ГБ Unified Memory") == 128
    assert extract_ram_gb("Apple Mac mini M4 Pro 64Gb RAM 1TB") == 64
    assert extract_ram_gb("Apple Mac mini M4 24/1TB SSD") == 24
    assert extract_ram_gb("Mac Studio M2 Max 96GB/2TB") == 96
    assert extract_ram_gb("Mini PC 512GB SSD 2TB HDD") is None


@pytest.mark.asyncio
async def test_rozetka_2step_stub_hydration_seller_id_and_section_id() -> None:
    requested_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested_urls.append(url)
        if "search/api/v6" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "goods": [
                            {"id": 579054886},
                            {"id": 579054999},
                        ]
                    }
                },
            )
        if "goodsId=579054886" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": 579054886,
                        "title": "Apple Mac Studio M2 Ultra 128GB / 2TB",
                        "price": 189999,
                        "price_pcs": "189 999 ₴",
                        "href": "https://rozetka.com.ua/ua/apple_mac_studio/p579054886/",
                        "seller_id": 5,
                        "sell_status": "available",
                        "product": {"state": "new"},
                    }
                },
            )
        if "goodsId=579054999" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": 579054999,
                        "title": "Apple Mac Studio M2 Max 64GB / 1TB Refurb",
                        "price": 119000,
                        "price_pcs": "119 000 ₴",
                        "href": "/ua/apple_mac_studio/p579054999/",
                        "seller_id": 128603,
                        "sell_status": "available",
                        "product": {"state": "refurbished"},
                    }
                },
            )
        return httpx.Response(404, text="Not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = RozetkaConnector(client=client)
    task = ShoppingTask(
        task_id="t_rz_hydrate",
        raw_query="Mac Studio 128GB",
        category="ai_mini_pc",
        allowed_conditions=[ConditionCode.BRAND_NEW, ConditionCode.OPEN_BOX, ConditionCode.REFURBISHED],
    )

    res = await connector.search(task)
    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 2

    # Verify section_id injection for ai_mini_pc
    search_reqs = [u for u in requested_urls if "search/api/v6" in u]
    assert len(search_reqs) == 1
    assert "section_id=80095" in search_reqs[0]

    by_id = {o.offer_id: o for o in res.offers}
    official = by_id["rozetka_579054886"]
    assert official.trust_tier == ChannelTrustTier.TIER_1_OFFICIAL
    assert official.store_name == "Rozetka.ua"
    assert official.condition.code == ConditionCode.BRAND_NEW

    mkt = by_id["rozetka_579054999"]
    assert mkt.trust_tier == ChannelTrustTier.TIER_2_ESCROW
    assert mkt.store_name == "Rozetka Marketplace (#128603)"
    assert mkt.condition.code == ConditionCode.REFURBISHED


@pytest.mark.asyncio
async def test_olx_geo_pickup_routing_for_kyiv_and_zhytomyr() -> None:
    requested_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested_urls.append(url)
        if "/ko/" in url:
            html = """
            <div data-cy="l-card">
              <a href="/d/uk/obyavlenie/mac-studio-kyiv-IDKyiv1.html"><h4>Apple Mac Studio M2 Ultra 128GB</h4></a>
              <p>145 000 грн</p>
              <span>Київ, Печерський - Сьогодні</span>
            </div>
            """
            return httpx.Response(200, text=html)
        html_zt = """
        <div data-cy="l-card">
          <a href="/d/uk/obyavlenie/mac-studio-zt-IDZhit1.html"><h4>Apple Mac Studio M2 Max 64GB</h4></a>
          <p>95 000 грн</p>
          <span>Житомир - Вчора</span>
        </div>
        """
        return httpx.Response(200, text=html_zt)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = OLXConnector(use_headless_chrome=False, client=client)
    task = ShoppingTask(
        task_id="t_olx_geo",
        raw_query="Mac Studio",
        pickup_regions=["Київ", "Житомир"],
        require_local_pickup=True,
    )
    res = await connector.search(task)
    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 2
    assert any("/uk/list/ko/q-Mac-Studio/" in u for u in requested_urls)
    assert any("/uk/list/zhitomirskaya-oblast/q-Mac-Studio/" in u for u in requested_urls)


def test_funnel_stage1_geo_pickup_noise_and_min_ram_gb() -> None:
    task = ShoppingTask(
        task_id="t_s1_hard",
        raw_query="Mac Studio 128GB unified memory",
        category="ai_mini_pc",
        allowed_conditions=[ConditionCode.BRAND_NEW, ConditionCode.OPEN_BOX, ConditionCode.USED_GOOD],
        pickup_regions=["київ", "житомир"],
        require_local_pickup=True,
        min_ram_gb=64,
    )

    valid_kyiv = _make_offer(
        "ok_kyiv",
        "Apple Mac Studio M2 Ultra 128GB / 2TB",
        145000.0,
        channel="olx",
        url="https://www.olx.ua/d/uk/obyavlenie/mac-studio-128gb-IDKyiv1.html",
        store_name="OLX (Київ, Печерський - Сьогодні)",
        trust_tier=ChannelTrustTier.TIER_2_ESCROW,
        raw_condition="Вживане",
        specs={"location": "Київ, Печерський - Сьогодні"},
    )
    valid_zhytomyr = _make_offer(
        "ok_zt",
        "Apple Mac mini M4 Pro 64Gb RAM 1TB",
        79000.0,
        channel="olx",
        url="https://www.olx.ua/d/uk/obyavlenie/mac-mini-64gb-IDZhit1.html",
        store_name="OLX (Житомир - Вчора)",
        trust_tier=ChannelTrustTier.TIER_2_ESCROW,
        raw_condition="Вживане",
        specs={"location": "Житомир - Вчора"},
    )
    pruned_if = _make_offer(
        "bad_if",
        "Apple Mac Studio M2 Ultra 128GB",
        130000.0,
        channel="olx",
        url="https://www.olx.ua/d/uk/obyavlenie/mac-studio-if-IDIf1.html",
        store_name="OLX (Івано-Франківськ - Сьогодні)",
        trust_tier=ChannelTrustTier.TIER_2_ESCROW,
        specs={"location": "Івано-Франківськ - Сьогодні"},
    )
    pruned_lviv = _make_offer(
        "bad_lviv",
        "Apple Mac Studio M2 Max 64GB",
        98000.0,
        channel="olx",
        url="https://www.olx.ua/d/uk/obyavlenie/mac-studio-lviv-IDLviv1.html",
        store_name="OLX (Львів - Сьогодні)",
        trust_tier=ChannelTrustTier.TIER_2_ESCROW,
        specs={"location": "Львів - Сьогодні"},
    )
    pruned_noise_lcd = _make_offer(
        "bad_lcd",
        "Дисплей телефона 6.43 LCD AMOLED матриця",
        2400.0,
        channel="prom",
        url="https://prom.ua/ua/p12345-lcd-643.html",
    )
    pruned_noise_bag = _make_offer(
        "bad_bag",
        "Сумка для Mac mini ударостійка",
        1200.0,
        channel="rozetka",
        url="https://rozetka.com.ua/ua/bag-mac-mini/p123456/",
    )
    pruned_barebone = _make_offer(
        "bad_barebone",
        "Minisforum Barebone без ОЗП Ryzen AI Max+ 395",
        45000.0,
        channel="rozetka",
        url="https://rozetka.com.ua/ua/minisforum-barebone/p987654/",
    )
    pruned_low_ram = _make_offer(
        "bad_24gb",
        "Apple Mac mini M4 24/1TB",
        49000.0,
        channel="rozetka",
        url="https://rozetka.com.ua/ua/apple-mac-mini-24/p555111/",
    )
    pruned_search_url = _make_offer(
        "bad_url",
        "Apple Mac Studio M2 Ultra 128GB",
        150000.0,
        channel="rozetka",
        url="https://rozetka.com.ua/ua/search/?text=mac+studio",
    )

    report = StatisticalFunnelEngine.run(
        [
            valid_kyiv,
            valid_zhytomyr,
            pruned_if,
            pruned_lviv,
            pruned_noise_lcd,
            pruned_noise_bag,
            pruned_barebone,
            pruned_low_ram,
            pruned_search_url,
        ],
        task,
    )

    valid_ids = {o.offer_id for o in report.valid_candidates}
    pruned_by_id = {o.offer_id: o for o in report.pruned_offers}

    assert valid_ids == {"ok_kyiv", "ok_zt"}
    assert set(pruned_by_id.keys()) == {
        "bad_if",
        "bad_lviv",
        "bad_lcd",
        "bad_bag",
        "bad_barebone",
        "bad_24gb",
        "bad_url",
    }
    assert "радіусом" in pruned_by_id["bad_if"].prune_reason
    assert "радіусом" in pruned_by_id["bad_lviv"].prune_reason
    assert "мінімальну вимогу (64 ГБ)" in pruned_by_id["bad_24gb"].prune_reason
    assert "пошук або каталог" in pruned_by_id["bad_url"].prune_reason
    assert all(o.deal_badge == DealTierBadge.PRUNED_OUTLIER for o in report.pruned_offers)


def test_domain_spec_scorer_ai_mini_pc() -> None:
    task = OmniOrchestrator.build_task_from_query("Mac Studio 128GB unified memory AI Mini-PC")
    assert task.category == "ai_mini_pc"

    m2_ultra_128 = _make_offer(
        "m2u_128",
        "Apple Mac Studio M2 Ultra 128GB / 2TB SSD 10GbE",
        165000.0,
    )
    strix_halo_128 = _make_offer(
        "strix_128",
        "Framework Desktop Ryzen AI Max+ 395 Strix Halo 128GB USB4 Proxmox",
        115000.0,
    )
    m4_pro_64 = _make_offer(
        "m4p_64",
        "Apple Mac mini M4 Pro 64GB / 1TB Thunderbolt 5",
        85000.0,
    )
    m4_24 = _make_offer(
        "m4_24",
        "Apple Mac mini M4 24/1TB",
        49000.0,
    )

    DomainSpecScorer.score_offers([m2_ultra_128, strix_halo_128, m4_pro_64, m4_24], task)

    assert m2_ultra_128.quality_score > strix_halo_128.quality_score > m4_pro_64.quality_score > m4_24.quality_score
    assert m2_ultra_128.quality_score >= 90.0
    assert m4_24.quality_score < 45.0


@pytest.mark.asyncio
async def test_gemini_reviewer_memory_sqlite_no_error() -> None:
    reviewer = OptionalGeminiReviewer(api_key="", db_path=":memory:")
    res = await reviewer.review_shortlist(
        "t_mem",
        {"candidates": [{"title": "Apple Mac Studio M2 Ultra 128GB", "realistic_landed_uah": 165000}]},
    )
    assert res["source"] == "deterministic_local"
    assert "Apple Mac Studio" in res["summary_uk"]
