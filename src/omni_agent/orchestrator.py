from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any, Optional

from omni_agent.connectors.aliexpress import AliExpressConnector
from omni_agent.connectors.amazon_eu import AmazonEUConnector
from omni_agent.connectors.base import ChannelHarvestResult, ManualCheckLink
from omni_agent.connectors.ceneo_pl import CeneoPLConnector
from omni_agent.connectors.deep_links import SmartDeepLinkGenerator
from omni_agent.connectors.hotline import HotlineConnector
from omni_agent.connectors.idealo_de import IdealoDEConnector
from omni_agent.connectors.olx import OLXConnector
from omni_agent.connectors.prom import PromConnector
from omni_agent.connectors.rozetka_smart import RozetkaConnector
from omni_agent.core.condition_grader import ConditionGrader
from omni_agent.core.funnel import FunnelAuditReport, StatisticalFunnelEngine
from omni_agent.core.gemini_studio_opt import OptionalGeminiReviewer
from omni_agent.core.landed_cost import LandedCostEngine
from omni_agent.core.models import (
    ChannelTrustTier,
    ConditionCode,
    CustomsMode,
    NormalizedOffer,
    ShoppingTask,
    VerificationStatus,
)
from omni_agent.core.normalizer import extract_model_codes, normalize_title
from omni_agent.core.pareto_topsis import ParetoTopsisEngine
from omni_agent.storage.drive_backup import backup_sqlite_to_drive
from omni_agent.storage.gsheets_bridge import GoogleSheetsBridge, export_master_sheets_payload
from omni_agent.storage.sqlite_store import SQLiteStore
from omni_agent.ui.report_renderer import render_standalone_hitl_html


def _build_demo_offer(
    *,
    offer_id: str,
    task: ShoppingTask,
    channel: str,
    route_country: str,
    store_name: str,
    trust_tier: ChannelTrustTier,
    title: str,
    price_raw: float,
    currency: str,
    url: str,
    delivery_days: int,
    warranty_months: int,
    raw_condition: str = "",
    seller_rating: Optional[float] = 4.8,
    orders_or_reviews: int = 45,
    direct_to_ua_export: bool = False,
    shipping_raw: float = 0.0,
    local_escrow_shipping_uah: float = 0.0,
    specs: Optional[dict[str, Any]] = None,
    landed_engine: Optional[LandedCostEngine] = None,
) -> NormalizedOffer:
    engine = landed_engine or LandedCostEngine()
    cond = ConditionGrader.grade(title, raw_condition=raw_condition)
    lc = engine.calculate(
        item_price_raw=price_raw,
        currency=currency,
        route_country=route_country,
        shipping_raw=shipping_raw,
        weight_kg=task.estimated_weight_kg,
        volumetric_factor=task.volumetric_factor,
        customs_mode=task.customs_mode,
        direct_to_ua_export=direct_to_ua_export,
        local_escrow_shipping_uah=local_escrow_shipping_uah,
    )
    return NormalizedOffer(
        offer_id=offer_id,
        task_id=task.task_id,
        channel=channel,
        route_country=route_country,
        store_name=store_name,
        trust_tier=trust_tier,
        title=title,
        normalized_title=normalize_title(title),
        mpn_codes=extract_model_codes(title),
        url=url,
        price_raw=price_raw,
        currency=currency,
        shipping_raw=shipping_raw,
        in_stock=True,
        delivery_days=delivery_days,
        warranty_months=warranty_months,
        seller_rating=seller_rating,
        orders_or_reviews=orders_or_reviews,
        condition=cond,
        landed_cost=lc,
        verification_status=VerificationStatus.VERIFIED,
        raw_price_snippet=f"{price_raw:.2f} {currency}",
        is_grounded=True,
        specs=specs or {},
    )


class OmniOrchestrator:
    """
    End-to-end multi-channel orchestrator:
      1. Supports Ukrainian Local + Used/Open-Box mode (`ua_only=True`, `include_used=True`)
         as well as full global mode (`UA + AliExpress + Amazon EU + Idealo DE + Ceneo PL + Deep-Links`).
      2. Runs DomainSpecScorer -> StatisticalFunnelEngine -> ParetoTopsisEngine -> OptionalGeminiReviewer.
      3. Persists to SQLiteStore, optionally backs up to Drive / Google Sheets,
         and exports standalone HTML HITL portal + Markdown Market Briefing.
    """

    def __init__(
        self,
        *,
        db_path: str | Path = "data/shopping_hub.db",
        reports_dir: str | Path = "reports",
        landed_cost_engine: Optional[LandedCostEngine] = None,
        gemini_reviewer: Optional[OptionalGeminiReviewer] = None,
        gsheets_bridge: Optional[GoogleSheetsBridge] = None,
        demo_mode: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self.db_path = str(db_path)
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.landed_cost_engine = landed_cost_engine or LandedCostEngine()
        self.store = SQLiteStore(self.db_path)
        self.gemini_reviewer = gemini_reviewer or OptionalGeminiReviewer(
            db_path=Path(self.db_path).parent / "llm_eval_cache.sqlite"
            if self.db_path != ":memory:"
            else ":memory:"
        )
        self.gsheets_bridge = gsheets_bridge or GoogleSheetsBridge()
        self.deep_link_gen = SmartDeepLinkGenerator(landed_cost_engine=self.landed_cost_engine)
        self.demo_mode = demo_mode
        self.timeout = timeout

    def _prepare_task(
        self,
        task: ShoppingTask,
        *,
        include_used: bool = False,
    ) -> ShoppingTask:
        task_copy = task.model_copy(deep=True)
        if include_used:
            conds = list(task_copy.allowed_conditions)
            for c in (ConditionCode.OPEN_BOX, ConditionCode.USED_GOOD, ConditionCode.REFURBISHED):
                if c not in conds:
                    conds.append(c)
            task_copy.allowed_conditions = conds
        return task_copy

    def _build_used_query_task(self, task: ShoppingTask, suffix: str = "б/в") -> ShoppingTask:
        q_uk = task.queries_i18n.get("uk") or task.raw_query
        if suffix not in q_uk.lower():
            q_used = f"{q_uk} {suffix}"
        else:
            q_used = q_uk
        used_task = task.model_copy(deep=True)
        used_task.raw_query = q_used
        used_task.queries_i18n = {**used_task.queries_i18n, "uk": q_used}
        return used_task

    def _generate_demo_harvest(
        self,
        task: ShoppingTask,
        *,
        ua_only: bool,
        include_used: bool,
    ) -> tuple[list[NormalizedOffer], list[ManualCheckLink]]:
        q_low = f"{task.raw_query} {task.category} {' '.join(task.canonical_mpns)}".lower()
        offers: list[NormalizedOffer] = []
        manual_links: list[ManualCheckLink] = []

        is_power = (
            "power" in q_low
            or "station" in q_low
            or "станц" in q_low
            or "ecoflow" in q_low
            or "bluetti" in q_low
            or "lifepo4" in q_low
            or task.customs_mode == CustomsMode.ENERGY_EXEMPT
        )
        is_monitor = (
            "monitor" in q_low
            or "монітор" in q_low
            or "43" in q_low
            or "42.5" in q_low
            or "ips" in q_low
            or "43sq700" in q_low
            or "43un700" in q_low
            or "u4323qe" in q_low
            or "m7" in q_low
        )

        if is_power and not is_monitor:
            offers.extend(
                [
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_rozetka_ecoflow",
                        task=task,
                        channel="rozetka",
                        route_country="UA",
                        store_name="Rozetka Official",
                        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
                        title="Зарядна станція EcoFlow DELTA 2 Max 2048Wh LiFePO4 3000 cycles 15ms UPS",
                        price_raw=54999.0,
                        currency="UAH",
                        url="https://rozetka.com.ua/ua/ecoflow-delta-2-max/p389102001/",
                        delivery_days=1,
                        warranty_months=60,
                        specs={"battery_chemistry": "LiFePO4", "capacity_wh": 2048, "cycles_count": 3000, "ups_ms": 15, "noise_db": 35},
                        landed_engine=self.landed_cost_engine,
                    ),
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_hotline_bluetti",
                        task=task,
                        channel="hotline",
                        route_country="UA",
                        store_name="Hotline Catalog (Official UA)",
                        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
                        title="Зарядна станція Bluetti AC200L 2048Wh LiFePO4 3000+ cycles 20ms UPS",
                        price_raw=52500.0,
                        currency="UAH",
                        url="https://hotline.ua/ua/bluetti-ac200l/",
                        delivery_days=2,
                        warranty_months=60,
                        specs={"battery_chemistry": "LiFePO4", "capacity_wh": 2048, "cycles_count": 3000, "ups_ms": 20, "noise_db": 40},
                        landed_engine=self.landed_cost_engine,
                    ),
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_olx_station_openbox",
                        task=task,
                        channel="olx",
                        route_country="UA",
                        store_name="OLX (Київ, Печерськ)",
                        trust_tier=ChannelTrustTier.TIER_2_ESCROW,
                        title="EcoFlow DELTA 2 Max 2048Wh LiFePO4 Open Box стан нового перевірка на місці",
                        price_raw=38500.0,
                        currency="UAH",
                        url="https://www.olx.ua/d/uk/ecoflow-delta-2-max-openbox/",
                        delivery_days=1,
                        warranty_months=12,
                        raw_condition="Open Box",
                        local_escrow_shipping_uah=450.0,
                        specs={
                            "battery_chemistry": "LiFePO4",
                            "capacity_wh": 2048,
                            "inspection_tip_uk": "Перевірити 100% зарядку/розрядку під навантаженням 1.5кВт та серійний номер у додатку.",
                        },
                        landed_engine=self.landed_cost_engine,
                    ),
                ]
            )
            if not ua_only:
                offers.append(
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_amz_de_delta2max",
                        task=task,
                        channel="amazon_de",
                        route_country="DE",
                        store_name="Amazon DE",
                        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
                        title="EcoFlow DELTA 2 Max 2048Wh LiFePO4 Portable Power Station EU 230V",
                        price_raw=999.0,
                        currency="EUR",
                        url="https://www.amazon.de/dp/B0C4DEMO",
                        delivery_days=8,
                        warranty_months=60,
                        direct_to_ua_export=True,
                        shipping_raw=35.0,
                        specs={"battery_chemistry": "LiFePO4", "capacity_wh": 2048, "cycles_count": 3000, "ups_ms": 15},
                        landed_engine=self.landed_cost_engine,
                    )
                )
        else:
            # Default / Monitors 42.5-43" LG IPS vs Samsung 43" VA showcase
            offers.extend(
                [
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_rozetka_lg_43sq700s",
                        task=task,
                        channel="rozetka",
                        route_country="UA",
                        store_name="Rozetka Official",
                        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
                        title='Монітор 42.5" LG 43SQ700S-W 4K UHD IPS Flicker-Safe USB-C 65W Smart Remote',
                        price_raw=18999.0,
                        currency="UAH",
                        url="https://rozetka.com.ua/ua/lg-43sq700s-w/p389102002/",
                        delivery_days=1,
                        warranty_months=24,
                        seller_rating=4.9,
                        orders_or_reviews=142,
                        specs={
                            "panel_type": "IPS",
                            "diagonal_inch": 42.5,
                            "flicker_free": True,
                            "usb_c": True,
                            "remote_control": True,
                        },
                        landed_engine=self.landed_cost_engine,
                    ),
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_hotline_lg_43un700",
                        task=task,
                        channel="hotline",
                        route_country="UA",
                        store_name="Hotline Catalog (Official UA)",
                        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
                        title='Монітор 42.5" LG 43UN700-B 4K UHD IPS Flicker-Safe Hardware Low Blue Light USB-C 60W KVM',
                        price_raw=23499.0,
                        currency="UAH",
                        url="https://hotline.ua/ua/lg-43un700-b/",
                        delivery_days=2,
                        warranty_months=36,
                        seller_rating=4.8,
                        orders_or_reviews=38,
                        specs={
                            "panel_type": "IPS",
                            "diagonal_inch": 42.5,
                            "flicker_free": True,
                            "hardware_low_blue_light": True,
                            "usb_c": True,
                            "kvm": True,
                        },
                        landed_engine=self.landed_cost_engine,
                    ),
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_hotline_samsung_m7_va",
                        task=task,
                        channel="hotline",
                        route_country="UA",
                        store_name="Hotline Catalog (Official UA)",
                        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
                        title='Монітор 43" Samsung Smart Monitor M7 LS43DM702UUXUA 4K UHD VA USB-C',
                        price_raw=17499.0,
                        currency="UAH",
                        url="https://hotline.ua/ua/samsung-m7-43/",
                        delivery_days=1,
                        warranty_months=24,
                        seller_rating=4.7,
                        orders_or_reviews=85,
                        specs={
                            "panel_type": "VA",
                            "diagonal_inch": 43.0,
                            "usb_c": True,
                            "inspection_tip_uk": "VA матриця: кращий чорний у кіно, але гірші кути огляду для статичного тексту порівняно з LG IPS.",
                        },
                        landed_engine=self.landed_cost_engine,
                    ),
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_prom_lg_43sq700s_openbox",
                        task=task,
                        channel="prom",
                        route_country="UA",
                        store_name="OutletMonitor UA (Prom-оплата)",
                        trust_tier=ChannelTrustTier.TIER_2_ESCROW,
                        title='Монітор 42.5" LG 43SQ700S-W 4K UHD IPS Open Box вітринний зразок ідеальний стан',
                        price_raw=14800.0,
                        currency="UAH",
                        url="https://prom.ua/ua/p9994301-lg-43sq700s-openbox.html",
                        delivery_days=2,
                        warranty_months=12,
                        raw_condition="Open Box",
                        local_escrow_shipping_uah=140.0,
                        specs={
                            "panel_type": "IPS",
                            "diagonal_inch": 42.5,
                            "flicker_free": True,
                            "usb_c": True,
                            "inspection_tip_uk": "Вітринний екземпляр з Prom-оплатою — перевірити білі/чорні заливки на Новій Пошті.",
                        },
                        landed_engine=self.landed_cost_engine,
                    ),
                    # Trap items to verify Stage 1 & Stage 3 funnel audit
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_olx_trap_cable",
                        task=task,
                        channel="olx",
                        route_country="UA",
                        store_name="OLX Аксесуари",
                        trust_tier=ChannelTrustTier.TIER_2_ESCROW,
                        title="Кронштейн настінний та кабель USB-C для монітора LG 43SQ700S",
                        price_raw=950.0,
                        currency="UAH",
                        url="https://www.olx.ua/d/uk/mount-lg-43/",
                        delivery_days=2,
                        warranty_months=0,
                        landed_engine=self.landed_cost_engine,
                    ),
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_olx_trap_broken",
                        task=task,
                        channel="olx",
                        route_country="UA",
                        store_name="OLX Київ",
                        trust_tier=ChannelTrustTier.TIER_2_ESCROW,
                        title="Монітор LG 43UN700-B 4K бита матриця на запчастини",
                        price_raw=3200.0,
                        currency="UAH",
                        url="https://www.olx.ua/d/uk/broken-lg-43/",
                        delivery_days=2,
                        warranty_months=0,
                        landed_engine=self.landed_cost_engine,
                    ),
                ]
            )

            if include_used:
                offers.extend(
                    [
                        _build_demo_offer(
                            offer_id=f"{task.task_id}_olx_used_lg_43un700",
                            task=task,
                            channel="olx",
                            route_country="UA",
                            store_name="OLX (Київ, Оболонь — самовивіз)",
                            trust_tier=ChannelTrustTier.TIER_2_ESCROW,
                            title='Монітор LG 43UN700-B 42.5" 4K IPS б/в у чудовому стані, живий огляд пікселів Київ',
                            price_raw=12900.0,
                            currency="UAH",
                            url="https://www.olx.ua/d/uk/lg-43un700-kyiv-used/",
                            delivery_days=1,
                            warranty_months=0,
                            raw_condition="Вживане",
                            local_escrow_shipping_uah=200.0,
                            specs={
                                "panel_type": "IPS",
                                "diagonal_inch": 42.5,
                                "flicker_free": True,
                                "usb_c": True,
                                "kvm": True,
                                "inspection_tip_uk": "Оглянути вживу в Києві: тест бітих пікселів, засвітів IPS glow та USB-C 60W.",
                            },
                            landed_engine=self.landed_cost_engine,
                        ),
                        _build_demo_offer(
                            offer_id=f"{task.task_id}_olx_used_samsung_m7",
                            task=task,
                            channel="olx",
                            route_country="UA",
                            store_name="OLX (Київ, Дарниця — OLX Доставка)",
                            trust_tier=ChannelTrustTier.TIER_2_ESCROW,
                            title='Samsung Smart Monitor M7 43" LS43DM702 4K VA б/в з пультом та коробкою',
                            price_raw=11400.0,
                            currency="UAH",
                            url="https://www.olx.ua/d/uk/samsung-m7-43-used/",
                            delivery_days=2,
                            warranty_months=0,
                            raw_condition="Вживане",
                            local_escrow_shipping_uah=250.0,
                            specs={
                                "panel_type": "VA",
                                "diagonal_inch": 43.0,
                                "usb_c": True,
                                "remote_control": True,
                                "inspection_tip_uk": "Перевірити матрицю на вертикальний бандинг та роботу Smart TV пульта.",
                            },
                            landed_engine=self.landed_cost_engine,
                        ),
                    ]
                )

            if not ua_only:
                offers.append(
                    _build_demo_offer(
                        offer_id=f"{task.task_id}_ceneo_pl_lg_43sq700s",
                        task=task,
                        channel="ceneo_pl",
                        route_country="PL",
                        store_name="Ceneo.pl (8 sklepów)",
                        trust_tier=ChannelTrustTier.TIER_1_OFFICIAL,
                        title='LG 43SQ700S-W 42.5" 4K UHD IPS Flicker-Safe USB-C',
                        price_raw=1599.0,
                        currency="PLN",
                        url="https://www.ceneo.pl/15499000",
                        delivery_days=6,
                        warranty_months=24,
                        specs={"panel_type": "IPS", "diagonal_inch": 42.5, "flicker_free": True, "usb_c": True},
                        landed_engine=self.landed_cost_engine,
                    )
                )

        # Estimate best UA new price for breakeven hints in manual check links
        ua_new_prices = [
            o.landed_cost.realistic_landed_uah
            for o in offers
            if o.route_country == "UA" and o.condition.code == ConditionCode.BRAND_NEW
        ]
        best_ua = min(ua_new_prices) if ua_new_prices else (task.target_price_uah or task.max_budget_uah)

        manual_links.extend(
            self.deep_link_gen.generate_ua_secondary_links(task, best_ua_uah=best_ua)
        )
        if not ua_only:
            manual_links.extend(
                self.deep_link_gen.generate(task, best_ua_uah=best_ua)
            )
        return offers, manual_links

    async def _harvest_live(
        self,
        task: ShoppingTask,
        *,
        ua_only: bool,
        include_used: bool,
    ) -> tuple[list[NormalizedOffer], list[ManualCheckLink]]:
        hotline = HotlineConnector(landed_cost_engine=self.landed_cost_engine, timeout=self.timeout)
        rozetka = RozetkaConnector(landed_cost_engine=self.landed_cost_engine, timeout=self.timeout)
        prom = PromConnector(landed_cost_engine=self.landed_cost_engine, timeout=self.timeout)
        olx = OLXConnector(landed_cost_engine=self.landed_cost_engine, timeout=self.timeout)

        # Wave 1: Ukrainian anchor discovery + MPN enrichment
        wave1_coros = [
            hotline.search(task),
            rozetka.search(task),
            prom.search(task),
            olx.search(task),
        ]
        if include_used:
            used_task = self._build_used_query_task(task, suffix="б/в")
            wave1_coros.append(olx.search(used_task))
            wave1_coros.append(prom.search(used_task))

        wave1_results: list[ChannelHarvestResult] = await asyncio.gather(*wave1_coros)

        all_offers: list[NormalizedOffer] = []
        manual_links: list[ManualCheckLink] = []
        seen_offer_ids: set[str] = set()
        seen_manual_urls: set[str] = set()

        discovered_mpns = list(task.canonical_mpns)
        for res in wave1_results:
            for code in res.discovered_mpns:
                if code not in discovered_mpns:
                    discovered_mpns.append(code)
            for off in res.offers:
                if off.offer_id not in seen_offer_ids:
                    seen_offer_ids.add(off.offer_id)
                    all_offers.append(off)
            if res.manual_link is not None and res.manual_link.direct_search_url not in seen_manual_urls:
                seen_manual_urls.add(res.manual_link.direct_search_url)
                manual_links.append(res.manual_link)

        enriched_task = task.model_copy(deep=True)
        enriched_task.canonical_mpns = discovered_mpns

        ua_prices = [
            o.landed_cost.realistic_landed_uah
            for o in all_offers
            if o.route_country == "UA" and o.condition.code == ConditionCode.BRAND_NEW
        ]
        best_ua_uah = min(ua_prices) if ua_prices else (task.target_price_uah or task.max_budget_uah)

        # Always add Ukrainian Secondary / Refurbished / Outlet smart inspection links
        for ulink in self.deep_link_gen.generate_ua_secondary_links(enriched_task, best_ua_uah=best_ua_uah):
            if ulink.direct_search_url not in seen_manual_urls:
                seen_manual_urls.add(ulink.direct_search_url)
                manual_links.append(ulink)

        if not ua_only:
            ali = AliExpressConnector(route_country="UA", landed_cost_engine=self.landed_cost_engine, timeout=self.timeout)
            amz_de = AmazonEUConnector(country="DE", landed_cost_engine=self.landed_cost_engine, timeout=self.timeout)
            idealo = IdealoDEConnector(landed_cost_engine=self.landed_cost_engine, timeout=self.timeout)
            ceneo = CeneoPLConnector(landed_cost_engine=self.landed_cost_engine, timeout=self.timeout)

            wave2_results: list[ChannelHarvestResult] = await asyncio.gather(
                ali.search(enriched_task, best_ua_uah=best_ua_uah),
                amz_de.search(enriched_task, best_ua_uah=best_ua_uah),
                idealo.search(enriched_task, best_ua_uah=best_ua_uah),
                ceneo.search(enriched_task, best_ua_uah=best_ua_uah),
            )
            for res in wave2_results:
                for off in res.offers:
                    if off.offer_id not in seen_offer_ids:
                        seen_offer_ids.add(off.offer_id)
                        all_offers.append(off)
                if res.manual_link is not None and res.manual_link.direct_search_url not in seen_manual_urls:
                    seen_manual_urls.add(res.manual_link.direct_search_url)
                    manual_links.append(res.manual_link)

            for glink in self.deep_link_gen.generate(enriched_task, best_ua_uah=best_ua_uah):
                if glink.direct_search_url not in seen_manual_urls:
                    seen_manual_urls.add(glink.direct_search_url)
                    manual_links.append(glink)

        return all_offers, manual_links

    @staticmethod
    def build_market_briefing_markdown(
        task: ShoppingTask,
        audit: FunnelAuditReport,
        top_chart: dict[str, Any],
        ranked_offers: list[NormalizedOffer],
        manual_links: list[ManualCheckLink],
        llm_review: dict[str, Any],
        *,
        ua_only: bool = False,
        include_used: bool = False,
    ) -> str:
        mode_flags = []
        if ua_only:
            mode_flags.append("🇺🇦 Тільки Україна (--ua-only)")
        else:
            mode_flags.append("🌍 Глобальний (UA + EU + AliExpress)")
        if include_used:
            mode_flags.append("🔍 З б/в та Open-Box (--include-used)")

        lines = [
            f"# Market Briefing: {task.raw_query}",
            f"- **Task ID:** `{task.task_id}`",
            f"- **Режим:** {' | '.join(mode_flags)}",
            f"- **Опорна медіана UA:** {round(audit.reference_median_uah)} ₴ (IQR: {round(audit.iqr_uah)} ₴)",
            f"- **Аудит воронки:** {audit.pruned_summary_uk}",
            "",
        ]
        if llm_review.get("summary_uk"):
            lines.append(f"> **Аудит ({llm_review.get('source', 'local')}):** {llm_review['summary_uk']}")
            lines.append("")

        lines.append("## Топ-кандидати (TOPSIS)")
        for idx, off in enumerate(ranked_offers[:8], start=1):
            lines.append(
                f"{idx}. **[{off.deal_badge.value}]** {off.title} — "
                f"**{round(off.landed_cost.realistic_landed_uah)} ₴** "
                f"({off.channel}/{off.route_country}, {off.condition.code.value}, "
                f"TOPSIS={off.topsis_score:.3f}, якість={off.effective_quality_score:.1f})"
            )

        dilemmas = top_chart.get("hard_tradeoff_dilemmas") or []
        if dilemmas:
            lines.append("")
            lines.append("## Жорсткі компроміси (Hard Trade-Offs)")
            for d in dilemmas[:5]:
                lines.append(f"- {d['dilemma_uk']}")

        ua_links = [l for l in manual_links if l.route_country == "UA"]
        if ua_links:
            lines.append("")
            lines.append("## Українські посилання для ручної перевірки (б/в, уцінка, живий огляд)")
            for l in ua_links:
                lines.append(f"- **{l.channel}**: {l.breakeven_hint_uk} → {l.direct_search_url}")

        return "\n".join(lines) + "\n"

    async def run_task(
        self,
        task: ShoppingTask,
        *,
        ua_only: bool = False,
        include_used: bool = False,
        demo_mode: Optional[bool] = None,
        injected_offers: Optional[list[NormalizedOffer]] = None,
        injected_manual_links: Optional[list[ManualCheckLink]] = None,
        export_html_path: Optional[str | Path] = None,
        push_to_gsheets: bool = False,
        backup_to_drive: bool = False,
        drive_backup_dir: Optional[str] = None,
    ) -> dict[str, Any]:
        use_demo = self.demo_mode if demo_mode is None else demo_mode
        prepared_task = self._prepare_task(task, include_used=include_used)
        self.store.upsert_task(prepared_task)

        if injected_offers is not None:
            raw_offers = list(injected_offers)
            manual_links = list(injected_manual_links or [])
            if not manual_links:
                manual_links = self.deep_link_gen.generate_ua_secondary_links(prepared_task)
                if not ua_only:
                    manual_links.extend(self.deep_link_gen.generate(prepared_task))
        elif use_demo:
            raw_offers, manual_links = self._generate_demo_harvest(
                prepared_task,
                ua_only=ua_only,
                include_used=include_used,
            )
        else:
            raw_offers, manual_links = await self._harvest_live(
                prepared_task,
                ua_only=ua_only,
                include_used=include_used,
            )

        audit = StatisticalFunnelEngine.run(raw_offers, prepared_task, rescore_specs=True)

        # Compute Pareto frontier & TOPSIS ranking over valid_candidates
        ParetoTopsisEngine.compute_pareto_frontier(audit.valid_candidates)
        ranked_offers = ParetoTopsisEngine.rank_with_topsis(audit.valid_candidates)
        top_chart = ParetoTopsisEngine.build_top_chart_quartet(
            ranked_offers=ranked_offers,
            human_review_deals=audit.human_review_deals,
            reference_median_uah=audit.reference_median_uah,
        )

        # Optional Gemini review (0 LLM calls when GEMINI_API_KEY is unset)
        shortlist_payload = {
            "query": prepared_task.raw_query,
            "reference_median_uah": audit.reference_median_uah,
            "candidates": [
                {
                    "offer_id": o.offer_id,
                    "title": o.title,
                    "channel": o.channel,
                    "route_country": o.route_country,
                    "condition": o.condition.code.value,
                    "realistic_landed_uah": o.landed_cost.realistic_landed_uah,
                    "effective_quality_score": o.effective_quality_score,
                    "topsis_score": o.topsis_score,
                }
                for o in ranked_offers[:5]
            ],
        }
        llm_review = await self.gemini_reviewer.review_shortlist(prepared_task.task_id, shortlist_payload)

        alerts = self.store.save_task_results(
            task_id=prepared_task.task_id,
            ranked_offers=ranked_offers,
            manual_links=manual_links,
            audit=audit,
            top_chart=top_chart,
        )

        prepared_task.status = "DONE"
        self.store.upsert_task(prepared_task)

        bundle = self.store.get_task_bundle(prepared_task.task_id)
        briefing_md = self.build_market_briefing_markdown(
            prepared_task,
            audit,
            top_chart,
            ranked_offers,
            manual_links,
            llm_review,
            ua_only=ua_only,
            include_used=include_used,
        )
        bundle["alerts"] = alerts
        bundle["llm_review"] = llm_review
        bundle["briefing_md"] = briefing_md

        # Export Markdown briefing & standalone HTML report
        briefing_path = self.reports_dir / f"{prepared_task.task_id}_briefing.md"
        briefing_path.write_text(briefing_md, encoding="utf-8")

        html_content = render_standalone_hitl_html(bundle)
        latest_html_path = Path(export_html_path) if export_html_path else (self.reports_dir / "latest_report.html")
        latest_html_path.parent.mkdir(parents=True, exist_ok=True)
        latest_html_path.write_text(html_content, encoding="utf-8")

        task_html_path = self.reports_dir / f"{prepared_task.task_id}_report.html"
        if task_html_path.resolve() != latest_html_path.resolve():
            task_html_path.write_text(html_content, encoding="utf-8")

        sheets_payload = export_master_sheets_payload(bundle)
        gsheets_sync_res: Optional[dict[str, Any]] = None
        if push_to_gsheets and self.gsheets_bridge.enabled:
            gsheets_sync_res = await self.gsheets_bridge.push_harvest_results(sheets_payload)

        backup_path: Optional[Path] = None
        if backup_to_drive:
            backup_path = backup_sqlite_to_drive(self.db_path, backup_dir=drive_backup_dir)

        bundle["reports"] = {
            "latest_html": str(latest_html_path),
            "task_html": str(task_html_path),
            "briefing_md": str(briefing_path),
        }
        bundle["sheets_payload"] = sheets_payload
        bundle["gsheets_sync"] = gsheets_sync_res
        bundle["drive_backup_path"] = str(backup_path) if backup_path else None
        return bundle

    @staticmethod
    def build_task_from_query(
        query: str,
        *,
        category: str = "general",
        max_budget_uah: Optional[float] = None,
        target_price_uah: Optional[float] = None,
        estimated_weight_kg: float = 1.5,
        include_used: bool = False,
        customs_mode: Optional[CustomsMode] = None,
    ) -> ShoppingTask:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", query.lower()).strip("_")[:28]
        short_hash = hashlib.sha1(query.encode("utf-8")).hexdigest()[:6]
        task_id = f"task_{slug}_{short_hash}" if slug else f"task_{short_hash}"
        mpns = extract_model_codes(query)

        q_low = f"{category} {query}".lower()
        inferred_cat = category
        if inferred_cat == "general":
            if any(k in q_low for k in ("монітор", "monitor", "43", "ips", "uhd", "4k", "43sq700", "43un700", "u4323qe")):
                inferred_cat = "monitors_large_format"
            elif any(k in q_low for k in ("станція", "station", "ecoflow", "bluetti", "lifepo4", "kwh")):
                inferred_cat = "power_stations"
            elif any(
                k in q_low
                for k in (
                    "mac studio",
                    "mac mini",
                    "strix halo",
                    "ai max",
                    "395",
                    "evo-x2",
                    "ms-s1",
                    "dgx spark",
                    "unified memory",
                    "міні-пк",
                    " неттоп",
                )
            ):
                inferred_cat = "ai_mini_pc"

        inferred_customs = customs_mode
        if inferred_customs is None:
            inferred_customs = (
                CustomsMode.ENERGY_EXEMPT if inferred_cat == "power_stations" else CustomsMode.STANDARD_150EUR
            )

        allowed_conds = [ConditionCode.BRAND_NEW, ConditionCode.OPEN_BOX]
        if include_used:
            allowed_conds.extend([ConditionCode.USED_GOOD, ConditionCode.REFURBISHED])

        return ShoppingTask(
            task_id=task_id,
            raw_query=query,
            canonical_mpns=mpns,
            queries_i18n={"uk": query, "en": query},
            category=inferred_cat,
            allowed_conditions=allowed_conds,
            max_budget_uah=max_budget_uah,
            target_price_uah=target_price_uah,
            estimated_weight_kg=estimated_weight_kg,
            customs_mode=inferred_customs,
        )
