from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    MANUAL_CHECK_LINK = "MANUAL_CHECK_LINK"
    REJECTED_CONFIRMED = "REJECTED_CONFIRMED"


class DealTierBadge(str, Enum):
    VERIFIED_JACKPOT = "VERIFIED_JACKPOT"
    HUMAN_REVIEW_DEAL = "HUMAN_REVIEW_DEAL"
    PARETO_OPTIMAL = "PARETO_OPTIMAL"
    STANDARD_VALID = "STANDARD_VALID"
    QUARANTINE_LOW_TRUST = "QUARANTINE_LOW_TRUST"
    PRUNED_OUTLIER = "PRUNED_OUTLIER"


class ConditionCode(str, Enum):
    BRAND_NEW = "brand_new"
    OPEN_BOX = "open_box"
    REFURBISHED = "refurbished"
    USED_GOOD = "used_good"
    FOR_PARTS = "for_parts"

    @property
    def condition_factor(self) -> float:
        factors = {
            ConditionCode.BRAND_NEW: 1.0,
            ConditionCode.OPEN_BOX: 0.93,
            ConditionCode.REFURBISHED: 0.85,
            ConditionCode.USED_GOOD: 0.72,
            ConditionCode.FOR_PARTS: 0.30,
        }
        return factors[self]


class ChannelTrustTier(int, Enum):
    TIER_1_OFFICIAL = 1
    TIER_2_ESCROW = 2
    TIER_3_UNVERIFIED = 3


class CustomsMode(str, Enum):
    STANDARD_150EUR = "standard_150eur"
    ENERGY_EXEMPT = "energy_exempt"
    NONE = "none"


class Coupon(BaseModel):
    code: Optional[str] = None
    description: str = ""
    discount_amount: float = 0.0
    min_spend: float = 0.0
    currency: str = "USD"


class ConditionAssessment(BaseModel):
    code: ConditionCode
    grade: float = Field(..., ge=1.0, le=10.0)
    factor: float
    badge: str
    summary: str
    usage_counter: Optional[int] = None


class LandedCostResult(BaseModel):
    route_country: str
    direct_to_ua_export: bool = False
    item_price_raw: float
    currency: str
    coupon_discount_raw: float = 0.0
    applied_coupon_code: Optional[str] = None
    vat_deducted_uah: float = 0.0
    origin_shipping_uah: float = 0.0
    forwarder_fee_uah: float = 0.0
    customs_duty_vat_uah: float = 0.0
    broker_fee_uah: float = 0.0
    fx_spread_uah: float = 0.0
    local_escrow_shipping_uah: float = 0.0
    base_landed_uah: float
    realistic_landed_uah: float
    billable_weight_kg: float = 0.5
    risk_flags: list[str] = Field(default_factory=list)
    notes_uk: str = ""


class ShoppingTask(BaseModel):
    task_id: str
    raw_query: str
    canonical_mpns: list[str] = Field(default_factory=list)
    queries_i18n: dict[str, str] = Field(default_factory=dict)
    category: str = "general"
    allowed_conditions: list[ConditionCode] = Field(
        default_factory=lambda: [ConditionCode.BRAND_NEW, ConditionCode.OPEN_BOX]
    )
    max_budget_uah: Optional[float] = None
    target_price_uah: Optional[float] = None
    urgency_days: int = 14
    estimated_weight_kg: float = 1.5
    volumetric_factor: float = 1.0
    customs_mode: CustomsMode = CustomsMode.STANDARD_150EUR
    pickup_regions: list[str] = Field(default_factory=list)
    require_local_pickup: bool = False
    min_ram_gb: Optional[int] = None
    status: str = "PENDING"
    human_notes: str = ""


class NormalizedOffer(BaseModel):
    offer_id: str
    task_id: str
    channel: str
    route_country: str = "UA"
    store_name: str = ""
    trust_tier: ChannelTrustTier = ChannelTrustTier.TIER_2_ESCROW
    title: str
    normalized_title: str = ""
    mpn_codes: list[str] = Field(default_factory=list)
    url: str
    image_url: str = ""
    price_raw: float = 0.0
    currency: str = "UAH"
    shipping_raw: float = 0.0
    in_stock: bool = True
    delivery_days: int = 3
    warranty_months: int = 12
    seller_rating: Optional[float] = None
    orders_or_reviews: int = 0
    coupons: list[Coupon] = Field(default_factory=list)
    condition: ConditionAssessment
    landed_cost: LandedCostResult
    verification_status: VerificationStatus = VerificationStatus.VERIFIED
    raw_price_snippet: str = ""
    is_grounded: bool = True
    specs: dict[str, Any] = Field(default_factory=dict)
    quality_score: float = 70.0
    effective_quality_score: float = 70.0
    channel_score: float = 75.0
    topsis_score: float = 0.5
    is_pareto: bool = False
    deal_badge: DealTierBadge = DealTierBadge.STANDARD_VALID
    discount_vs_median_pct: float = 0.0
    prune_reason: str = ""
    breakeven_hint_uk: str = ""
    tradeoff_summary_uk: str = ""
