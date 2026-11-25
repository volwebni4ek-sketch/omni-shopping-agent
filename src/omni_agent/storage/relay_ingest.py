from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field

from omni_agent.connectors.base import ManualCheckLink, build_manual_check_link
from omni_agent.core.compliance import ComplianceManager
from omni_agent.core.condition_grader import ConditionGrader
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

if TYPE_CHECKING:
    from omni_agent.orchestrator import OmniOrchestrator

DEFAULT_COUNTRY_CURRENCY: dict[str, str] = {
    "UA": "UAH",
    "DE": "EUR",
    "PL": "PLN",
    "CZ": "CZK",
    "FR": "EUR",
    "IT": "EUR",
    "ES": "EUR",
    "NL": "EUR",
    "GB": "GBP",
    "US": "USD",
    "CN": "USD",
}


class RelayOfferInput(BaseModel):
    offer_id: str = ""
    task_id: str = ""
    channel: str
    route_country: str = "UA"
    store_name: str = ""
    trust_tier: int = 2
    title: str
    url: str
    image_url: str = ""
    price_raw: float
    currency: str = ""
    shipping_raw: float = 0.0
    raw_price_snippet: str = ""
    raw_condition: str = ""
    in_stock: bool = True
    delivery_days: int = 3
    warranty_months: int = 12
    seller_rating: Optional[float] = None
    orders_or_reviews: int = 0
    location: str = ""
    verification_status: str = "VERIFIED"
    specs: dict[str, Any] = Field(default_factory=dict)


class RelayManualLinkInput(BaseModel):
    task_id: str = ""
    channel: str
    route_country: str = "UA"
    reason_status: str = "MANUAL_CHECK"
    breakeven_max_foreign_price: Optional[float] = None
    currency: str = "UAH"
    breakeven_hint_uk: str = ""
    direct_search_url: str


class RelayTaskBundle(BaseModel):
    task_id: str
    raw_query: str
    category: str = "general"
    canonical_mpns: list[str] = Field(default_factory=list)
    queries_i18n: dict[str, str] = Field(default_factory=dict)
    allowed_conditions: list[str] = Field(
        default_factory=lambda: ["brand_new", "open_box"]
    )
    max_budget_uah: Optional[float] = None
    target_price_uah: Optional[float] = None
    urgency_days: int = 14
    estimated_weight_kg: float = 1.5
    volumetric_factor: float = 1.0
    customs_mode: str = "standard_150eur"
    pickup_regions: list[str] = Field(default_factory=list)
    require_local_pickup: bool = False
    min_ram_gb: Optional[int] = None
    channels: list[str] = Field(default_factory=list)
    offers: list[RelayOfferInput] = Field(default_factory=list)
    manual_links: list[RelayManualLinkInput] = Field(default_factory=list)
    channel_summary: dict[str, Any] = Field(default_factory=dict)


class RelayBundle(BaseModel):
    schema_version: str = "1.0"
    generated_at: str = ""
    source_ip_mode: str = "residential_local"
    elapsed_sec: float = 0.0
    tasks: list[RelayTaskBundle] = Field(default_factory=list)


def _infer_category(raw_query: str, category: str) -> str:
    if category and category != "general":
        return category
    from omni_agent.orchestrator import OmniOrchestrator

    return OmniOrchestrator.build_task_from_query(raw_query, category=category or "general").category


def relay_task_to_shopping_task(relay_task: RelayTaskBundle | dict[str, Any]) -> ShoppingTask:
    rt = relay_task if isinstance(relay_task, RelayTaskBundle) else RelayTaskBundle.model_validate(relay_task)

    allowed: list[ConditionCode] = []
    for c in rt.allowed_conditions:
        try:
            allowed.append(ConditionCode(str(c).strip().lower()))
        except ValueError:
            continue
    if not allowed:
        allowed = [ConditionCode.BRAND_NEW, ConditionCode.OPEN_BOX]

    try:
        customs = CustomsMode(str(rt.customs_mode).strip().lower())
    except ValueError:
        customs = CustomsMode.STANDARD_150EUR

    mpns = list(rt.canonical_mpns)
    if not mpns:
        mpns = extract_model_codes(rt.raw_query)

    queries_i18n = dict(rt.queries_i18n)
    if rt.raw_query:
        queries_i18n.setdefault("uk", rt.raw_query)
        queries_i18n.setdefault("en", rt.raw_query)

    return ShoppingTask(
        task_id=rt.task_id,
        raw_query=rt.raw_query,
        canonical_mpns=mpns,
        queries_i18n=queries_i18n,
        category=_infer_category(rt.raw_query, rt.category),
        allowed_conditions=allowed,
        max_budget_uah=rt.max_budget_uah,
        target_price_uah=rt.target_price_uah,
        urgency_days=rt.urgency_days,
        estimated_weight_kg=rt.estimated_weight_kg,
        volumetric_factor=rt.volumetric_factor,
        customs_mode=customs,
        pickup_regions=list(rt.pickup_regions),
        require_local_pickup=rt.require_local_pickup,
        min_ram_gb=rt.min_ram_gb,
    )


def _resolve_trust_tier(raw_tier: int, channel: str) -> ChannelTrustTier:
    if raw_tier in (1, 2, 3):
        return ChannelTrustTier(raw_tier)
    ch = channel.lower()
    if ch in ("hotline", "idealo_de", "ceneo_pl") or ch.startswith("amazon"):
        return ChannelTrustTier.TIER_1_OFFICIAL
    return ChannelTrustTier.TIER_2_ESCROW


def relay_offer_to_normalized_offer(
    raw_offer: RelayOfferInput | dict[str, Any],
    task: ShoppingTask,
    *,
    landed_engine: Optional[LandedCostEngine] = None,
) -> NormalizedOffer:
    ro = raw_offer if isinstance(raw_offer, RelayOfferInput) else RelayOfferInput.model_validate(raw_offer)
    engine = landed_engine or LandedCostEngine()

    channel = (ro.channel or "unknown").strip().lower()
    route = (ro.route_country or "UA").strip().upper()
    currency = (ro.currency or DEFAULT_COUNTRY_CURRENCY.get(route, "UAH")).strip().upper()

    specs = dict(ro.specs or {})
    if ro.location and not specs.get("location"):
        specs["location"] = ro.location

    raw_snippet = (ro.raw_price_snippet or "").strip()
    if not raw_snippet and ro.price_raw > 0:
        raw_snippet = f"{ro.price_raw:.2f} {currency}"

    grounded, _ = ComplianceManager.verify_price_grounding(ro.price_raw, raw_snippet)

    cond = ConditionGrader.grade(
        ro.title,
        description=str(specs.get("presence") or specs.get("location") or ""),
        raw_condition=ro.raw_condition,
    )

    local_escrow_uah = 0.0
    direct_to_ua_export = False
    calc_route = route
    is_aliexpress_cn = channel == "aliexpress"

    if channel == "olx":
        local_escrow_uah = round(max(120.0, ro.price_raw * 0.015 + 100.0), 2)
    elif channel == "prom":
        local_escrow_uah = 70.0
    elif channel == "aliexpress":
        direct_to_ua_export = route == "UA"
        calc_route = "CN" if direct_to_ua_export else route

    if channel.startswith("amazon") and route == "DE":
        direct_ship = ro.shipping_raw if ro.shipping_raw > 0 else 25.0
        direct_lc = engine.calculate(
            item_price_raw=ro.price_raw,
            currency=currency,
            route_country="DE",
            shipping_raw=direct_ship,
            weight_kg=task.estimated_weight_kg,
            volumetric_factor=task.volumetric_factor,
            customs_mode=task.customs_mode,
            direct_to_ua_export=True,
        )
        fw_lc = engine.calculate(
            item_price_raw=ro.price_raw,
            currency=currency,
            route_country="DE",
            shipping_raw=0.0,
            weight_kg=task.estimated_weight_kg,
            volumetric_factor=task.volumetric_factor,
            customs_mode=task.customs_mode,
            direct_to_ua_export=False,
        )
        landed = direct_lc if direct_lc.realistic_landed_uah <= fw_lc.realistic_landed_uah else fw_lc
        shipping_raw = direct_ship if landed.direct_to_ua_export else 0.0
        specs.setdefault(
            "dual_route_comparison",
            {
                "direct_export_uah": direct_lc.realistic_landed_uah,
                "forwarder_uah": fw_lc.realistic_landed_uah,
                "chosen_mode": "direct_export" if landed is direct_lc else "forwarder_de",
                "savings_uah": round(abs(fw_lc.realistic_landed_uah - direct_lc.realistic_landed_uah), 2),
            },
        )
    else:
        shipping_raw = ro.shipping_raw
        landed = engine.calculate(
            item_price_raw=ro.price_raw,
            currency=currency,
            route_country=calc_route,
            shipping_raw=shipping_raw,
            weight_kg=task.estimated_weight_kg,
            volumetric_factor=task.volumetric_factor,
            customs_mode=task.customs_mode,
            direct_to_ua_export=direct_to_ua_export,
            local_escrow_shipping_uah=local_escrow_uah,
            is_aliexpress_cn=is_aliexpress_cn,
        )
        landed.route_country = route

    offer_id = (ro.offer_id or "").strip()
    if not offer_id:
        digest = hashlib.sha1(f"{channel}|{ro.url}".encode("utf-8")).hexdigest()[:10]
        offer_id = f"{channel}_{digest}"

    try:
        v_status = VerificationStatus(ro.verification_status)
    except ValueError:
        v_status = VerificationStatus.VERIFIED
    if not grounded:
        v_status = VerificationStatus.MANUAL_CHECK_LINK

    return NormalizedOffer(
        offer_id=offer_id,
        task_id=task.task_id,
        channel=channel,
        route_country=route,
        store_name=ro.store_name or channel.upper(),
        trust_tier=_resolve_trust_tier(ro.trust_tier, channel),
        title=ro.title,
        normalized_title=normalize_title(ro.title),
        mpn_codes=extract_model_codes(ro.title),
        url=ro.url,
        image_url=ro.image_url,
        price_raw=ro.price_raw,
        currency=currency,
        shipping_raw=shipping_raw,
        in_stock=ro.in_stock,
        delivery_days=ro.delivery_days,
        warranty_months=ro.warranty_months,
        seller_rating=ro.seller_rating,
        orders_or_reviews=ro.orders_or_reviews,
        condition=cond,
        landed_cost=landed,
        verification_status=v_status,
        raw_price_snippet=raw_snippet,
        is_grounded=grounded,
        specs=specs,
    )


def relay_manual_link_to_model(
    raw_link: RelayManualLinkInput | dict[str, Any],
    task: ShoppingTask,
    *,
    best_ua_uah: Optional[float] = None,
    landed_engine: Optional[LandedCostEngine] = None,
) -> ManualCheckLink:
    rl = raw_link if isinstance(raw_link, RelayManualLinkInput) else RelayManualLinkInput.model_validate(raw_link)
    route = (rl.route_country or "UA").strip().upper()
    currency = (rl.currency or DEFAULT_COUNTRY_CURRENCY.get(route, "UAH")).strip().upper()

    if rl.breakeven_max_foreign_price is not None and rl.breakeven_hint_uk:
        return ManualCheckLink(
            task_id=task.task_id,
            channel=rl.channel,
            route_country=route,
            reason_status=rl.reason_status,
            breakeven_max_foreign_price=rl.breakeven_max_foreign_price,
            currency=currency,
            breakeven_hint_uk=rl.breakeven_hint_uk,
            direct_search_url=rl.direct_search_url,
        )

    built = build_manual_check_link(
        task=task,
        channel=rl.channel,
        route_country=route,
        currency=currency,
        direct_search_url=rl.direct_search_url,
        reason_status=rl.reason_status,
        best_ua_uah=best_ua_uah,
        direct_to_ua_export=(route == "DE" and rl.channel.startswith("amazon")) or (route == "UA"),
        landed_cost_engine=landed_engine,
    )
    if rl.breakeven_hint_uk and not built.breakeven_max_foreign_price:
        built.breakeven_hint_uk = rl.breakeven_hint_uk
    return built


async def ingest_relay_bundle(
    bundle_data: RelayBundle | dict[str, Any],
    *,
    orchestrator: Optional[OmniOrchestrator] = None,
    db_path: str | Path = "data/shopping_hub.db",
    reports_dir: str | Path = "reports",
    ua_only: bool = False,
    include_used: bool = False,
    push_to_gsheets: bool = False,
    backup_to_drive: bool = False,
) -> list[dict[str, Any]]:
    relay_bundle = (
        bundle_data
        if isinstance(bundle_data, RelayBundle)
        else RelayBundle.model_validate(bundle_data)
    )
    if orchestrator is None:
        from omni_agent.orchestrator import OmniOrchestrator

        orch = OmniOrchestrator(db_path=db_path, reports_dir=reports_dir)
    else:
        orch = orchestrator
    engine = orch.landed_cost_engine

    results: list[dict[str, Any]] = []
    for r_task in relay_bundle.tasks:
        task = relay_task_to_shopping_task(r_task)
        offers = [
            relay_offer_to_normalized_offer(ro, task, landed_engine=engine)
            for ro in r_task.offers
        ]
        if ua_only:
            offers = [o for o in offers if o.route_country.upper() == "UA"]

        ua_new_prices = [
            o.landed_cost.realistic_landed_uah
            for o in offers
            if o.route_country == "UA" and o.condition.code == ConditionCode.BRAND_NEW and o.price_raw > 0
        ]
        best_ua_uah = min(ua_new_prices) if ua_new_prices else (task.target_price_uah or task.max_budget_uah)

        manual_links = [
            relay_manual_link_to_model(ml, task, best_ua_uah=best_ua_uah, landed_engine=engine)
            for ml in r_task.manual_links
        ]
        if ua_only:
            manual_links = [m for m in manual_links if m.route_country.upper() == "UA"]

        res = await orch.run_task(
            task,
            ua_only=ua_only,
            include_used=include_used,
            injected_offers=offers,
            injected_manual_links=manual_links,
            push_to_gsheets=push_to_gsheets,
            backup_to_drive=backup_to_drive,
        )
        results.append(res)

    return results


async def ingest_relay_file(
    path: str | Path,
    *,
    orchestrator: Optional[OmniOrchestrator] = None,
    db_path: str | Path = "data/shopping_hub.db",
    reports_dir: str | Path = "reports",
    ua_only: bool = False,
    include_used: bool = False,
    push_to_gsheets: bool = False,
    backup_to_drive: bool = False,
) -> list[dict[str, Any]]:
    relay_path = Path(path)
    raw = json.loads(relay_path.read_text(encoding="utf-8"))
    return await ingest_relay_bundle(
        raw,
        orchestrator=orchestrator,
        db_path=db_path,
        reports_dir=reports_dir,
        ua_only=ua_only,
        include_used=include_used,
        push_to_gsheets=push_to_gsheets,
        backup_to_drive=backup_to_drive,
    )
