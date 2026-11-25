from __future__ import annotations

from pydantic import BaseModel


class ForwarderRate(BaseModel):
    country_code: str
    provider_name: str
    base_fee_usd: float
    per_kg_usd: float
    min_weight_kg: float = 0.5
    delivery_days_est: int = 10


DEFAULT_FORWARDERS: dict[str, ForwarderRate] = {
    "PL": ForwarderRate(
        country_code="PL",
        provider_name="Meest Poland / NP Shopping PL",
        base_fee_usd=2.5,
        per_kg_usd=3.0,
        min_weight_kg=0.5,
        delivery_days_est=6,
    ),
    "DE": ForwarderRate(
        country_code="DE",
        provider_name="Meest Germany / NP Shopping DE",
        base_fee_usd=3.5,
        per_kg_usd=3.8,
        min_weight_kg=0.5,
        delivery_days_est=8,
    ),
    "CZ": ForwarderRate(
        country_code="CZ",
        provider_name="NP Shopping CZ",
        base_fee_usd=3.0,
        per_kg_usd=3.5,
        min_weight_kg=0.5,
        delivery_days_est=7,
    ),
    "FR": ForwarderRate(
        country_code="FR",
        provider_name="NP Shopping FR / Meest FR",
        base_fee_usd=4.0,
        per_kg_usd=4.5,
        min_weight_kg=0.5,
        delivery_days_est=10,
    ),
    "NL": ForwarderRate(
        country_code="NL",
        provider_name="Meest / NP Shopping NL",
        base_fee_usd=4.0,
        per_kg_usd=4.5,
        min_weight_kg=0.5,
        delivery_days_est=10,
    ),
    "ES": ForwarderRate(
        country_code="ES",
        provider_name="NP Shopping ES / Meest ES",
        base_fee_usd=4.0,
        per_kg_usd=4.5,
        min_weight_kg=0.5,
        delivery_days_est=11,
    ),
    "IT": ForwarderRate(
        country_code="IT",
        provider_name="NP Shopping IT / Meest IT",
        base_fee_usd=4.0,
        per_kg_usd=4.5,
        min_weight_kg=0.5,
        delivery_days_est=11,
    ),
    "GB": ForwarderRate(
        country_code="GB",
        provider_name="NP Shopping UK / Meest UK",
        base_fee_usd=4.5,
        per_kg_usd=5.0,
        min_weight_kg=0.5,
        delivery_days_est=12,
    ),
    "US": ForwarderRate(
        country_code="US",
        provider_name="NP Shopping US / Meest US",
        base_fee_usd=5.0,
        per_kg_usd=6.5,
        min_weight_kg=0.5,
        delivery_days_est=14,
    ),
}

EU_VAT_RATES: dict[str, float] = {
    "DE": 0.19,
    "PL": 0.23,
    "CZ": 0.21,
    "FR": 0.20,
    "IT": 0.22,
    "ES": 0.21,
    "NL": 0.21,
    "GB": 0.20,
}

DEFAULT_FX_RATES_UAH: dict[str, float] = {
    "USD": 41.5,
    "EUR": 45.0,
    "PLN": 10.5,
    "CZK": 1.78,
    "GBP": 52.5,
    "UAH": 1.0,
}

DEFAULT_FX_SPREAD_PCT: float = 0.02
MAJOR_FX_SPREAD_PCT: float = 0.01
CUSTOMS_THRESHOLD_EUR: float = 150.0
DEFAULT_BROKER_FEE_UAH: float = 350.0
