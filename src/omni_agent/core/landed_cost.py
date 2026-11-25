from __future__ import annotations

from typing import Any, Optional

from omni_agent.config.routes_and_rates import (
    CUSTOMS_THRESHOLD_EUR,
    DEFAULT_BROKER_FEE_UAH,
    DEFAULT_FORWARDERS,
    DEFAULT_FX_RATES_UAH,
    DEFAULT_FX_SPREAD_PCT,
    EU_VAT_RATES,
    MAJOR_FX_SPREAD_PCT,
    ForwarderRate,
)
from omni_agent.core.models import Coupon, CustomsMode, LandedCostResult


class LandedCostEngine:
    """
    Multi-Country Landed Cost Engine v2 for UA domestic, EU/US forwarders, and direct EU export.
    """

    def __init__(
        self,
        fx_rates_uah: Optional[dict[str, float]] = None,
        forwarders: Optional[dict[str, ForwarderRate]] = None,
        eu_vat_rates: Optional[dict[str, float]] = None,
        fx_spread_pct: float = DEFAULT_FX_SPREAD_PCT,
        major_fx_spread_pct: float = MAJOR_FX_SPREAD_PCT,
        broker_fee_uah: float = DEFAULT_BROKER_FEE_UAH,
    ) -> None:
        self.fx_rates_uah = {k.upper(): v for k, v in (fx_rates_uah or DEFAULT_FX_RATES_UAH).items()}
        self.forwarders = {k.upper(): v for k, v in (forwarders or DEFAULT_FORWARDERS).items()}
        self.eu_vat_rates = {k.upper(): v for k, v in (eu_vat_rates or EU_VAT_RATES).items()}
        self.fx_spread_pct = fx_spread_pct
        self.major_fx_spread_pct = major_fx_spread_pct
        self.broker_fee_uah = broker_fee_uah

    def _to_uah(self, amount: float, currency: str) -> float:
        cur = currency.upper()
        rate = self.fx_rates_uah.get(cur)
        if rate is None:
            raise ValueError(f"Unsupported currency: {currency}")
        return amount * rate

    def _convert_currency(self, amount: float, from_currency: str, to_currency: str) -> float:
        if from_currency.upper() == to_currency.upper():
            return amount
        uah_val = self._to_uah(amount, from_currency)
        target_rate = self.fx_rates_uah.get(to_currency.upper())
        if target_rate is None or target_rate <= 0:
            raise ValueError(f"Unsupported target currency: {to_currency}")
        return uah_val / target_rate

    def match_best_coupon(
        self,
        item_price_raw: float,
        currency: str,
        coupons: Optional[list[Coupon]] = None,
    ) -> tuple[float, Optional[str]]:
        if not coupons or item_price_raw <= 0:
            return 0.0, None

        best_discount = 0.0
        best_code: Optional[str] = None
        for c in coupons:
            if c.discount_amount <= 0:
                continue
            c_cur = (c.currency or currency).upper()
            min_spend_in_item_cur = self._convert_currency(c.min_spend, c_cur, currency) if c.min_spend > 0 else 0.0
            if item_price_raw < min_spend_in_item_cur:
                continue
            discount_in_item_cur = self._convert_currency(c.discount_amount, c_cur, currency)
            if discount_in_item_cur > best_discount:
                best_discount = discount_in_item_cur
                best_code = c.code or c.description or None

        best_discount = min(best_discount, item_price_raw)
        return round(best_discount, 4), best_code

    def calculate(
        self,
        item_price_raw: float,
        currency: str,
        route_country: str = "UA",
        shipping_raw: float = 0.0,
        weight_kg: float = 1.5,
        volumetric_factor: float = 1.0,
        customs_mode: CustomsMode = CustomsMode.STANDARD_150EUR,
        direct_to_ua_export: bool = False,
        coupons: Optional[list[Coupon]] = None,
        local_escrow_shipping_uah: float = 0.0,
        is_aliexpress_cn: bool = False,
    ) -> LandedCostResult:
        route = (route_country or "UA").upper()
        cur = (currency or "UAH").upper()

        coupon_discount_raw, applied_coupon_code = self.match_best_coupon(item_price_raw, cur, coupons)
        discounted_gross_raw = max(0.0, item_price_raw - coupon_discount_raw)

        discounted_gross_uah = self._to_uah(discounted_gross_raw, cur)
        origin_shipping_uah = self._to_uah(max(0.0, shipping_raw), cur)

        vat_deducted_uah = 0.0
        net_item_uah = discounted_gross_uah

        if route != "UA" and direct_to_ua_export and route in self.eu_vat_rates:
            vat_rate = self.eu_vat_rates[route]
            net_item_uah = discounted_gross_uah / (1.0 + vat_rate)
            vat_deducted_uah = discounted_gross_uah - net_item_uah

        # Forwarder shipping & billable weight
        billable_weight_kg = max(0.5, round(max(0.0, weight_kg) * max(0.1, volumetric_factor), 3))
        forwarder_fee_uah = 0.0

        if route != "UA" and not direct_to_ua_export:
            fw = self.forwarders.get(route)
            if fw is not None:
                billable_weight_kg = max(fw.min_weight_kg, round(max(0.0, weight_kg) * max(0.1, volumetric_factor), 3))
                forwarder_fee_usd = fw.base_fee_usd + fw.per_kg_usd * billable_weight_kg
                forwarder_fee_uah = self._to_uah(forwarder_fee_usd, "USD")
            elif route not in ("CN",):
                # Generic fallback forwarder for unlisted foreign countries
                forwarder_fee_usd = 4.0 + 4.0 * billable_weight_kg
                forwarder_fee_uah = self._to_uah(forwarder_fee_usd, "USD")

        # UA customs calculation (10% duty + 20% VAT = 32% effective on excess over 150 EUR)
        eur_rate = self.fx_rates_uah["EUR"]
        net_item_eur = net_item_uah / eur_rate
        customs_duty_vat_uah = 0.0

        if route != "UA" and customs_mode == CustomsMode.STANDARD_150EUR:
            if net_item_eur > CUSTOMS_THRESHOLD_EUR:
                excess_uah = (net_item_eur - CUSTOMS_THRESHOLD_EUR) * eur_rate
                duty_uah = excess_uah * 0.10
                vat_uah = (excess_uah + duty_uah) * 0.20
                customs_duty_vat_uah = duty_uah + vat_uah

        broker_fee_uah = self.broker_fee_uah if customs_duty_vat_uah > 0 else 0.0

        # FX spread applies to foreign currency settlements on realistic_landed_uah
        fx_spread_uah = 0.0
        foreign_spend_uah = net_item_uah + origin_shipping_uah + forwarder_fee_uah
        if cur != "UAH":
            if cur in ("EUR", "USD"):
                fx_spread_uah = foreign_spend_uah * self.major_fx_spread_pct
            else:
                fx_spread_uah = foreign_spend_uah * self.fx_spread_pct

        local_escrow_uah = max(0.0, local_escrow_shipping_uah)

        base_landed_uah = (
            net_item_uah
            + origin_shipping_uah
            + forwarder_fee_uah
            + customs_duty_vat_uah
            + local_escrow_uah
        )
        realistic_landed_uah = base_landed_uah + broker_fee_uah + fx_spread_uah

        # Risk flags
        risk_flags: list[str] = []
        if route != "UA" and customs_mode == CustomsMode.STANDARD_150EUR and 140.0 <= net_item_eur <= 155.0:
            risk_flags.append("BORDERLINE_150EUR_FX_RISK")

        if (
            route in self.eu_vat_rates
            and not direct_to_ua_export
            and customs_duty_vat_uah > 0
            and not is_aliexpress_cn
        ):
            risk_flags.append("DOUBLE_VAT_WARNING")

        if (
            route != "UA"
            and forwarder_fee_uah > 0
            and net_item_uah < 1000.0
            and forwarder_fee_uah > 0.30 * net_item_uah
        ):
            risk_flags.append("DEAD_ZONE_UNPROFITABLE")

        # Concise Ukrainian notes
        notes_parts: list[str] = []
        if route == "UA":
            notes_parts.append("Локальна доставка по Україні")
            if local_escrow_uah > 0:
                notes_parts.append(f"доставка/ескроу {round(local_escrow_uah)} ₴")
        elif direct_to_ua_export:
            notes_parts.append(f"Прямий експорт з {route} до України")
            if vat_deducted_uah > 0:
                notes_parts.append(f"віднято ПДВ {route} (-{round(vat_deducted_uah)} ₴)")
        else:
            fw = self.forwarders.get(route)
            fw_name = fw.provider_name if fw else f"Форвардер {route}"
            notes_parts.append(f"Через {fw_name} ({billable_weight_kg:.2f} кг)")

        if customs_mode == CustomsMode.ENERGY_EXEMPT and route != "UA":
            notes_parts.append("пільга 0% мито/ПДВ (енергообладнання)")
        elif customs_duty_vat_uah > 0:
            notes_parts.append(f"розмитнення >150€: {round(customs_duty_vat_uah)} ₴ + брокер {round(broker_fee_uah)} ₴")
        elif route != "UA":
            notes_parts.append("в межах безмитного ліміту 150€")

        return LandedCostResult(
            route_country=route,
            direct_to_ua_export=direct_to_ua_export,
            item_price_raw=round(item_price_raw, 2),
            currency=cur,
            coupon_discount_raw=round(coupon_discount_raw, 2),
            applied_coupon_code=applied_coupon_code,
            vat_deducted_uah=round(vat_deducted_uah, 2),
            origin_shipping_uah=round(origin_shipping_uah, 2),
            forwarder_fee_uah=round(forwarder_fee_uah, 2),
            customs_duty_vat_uah=round(customs_duty_vat_uah, 2),
            broker_fee_uah=round(broker_fee_uah, 2),
            fx_spread_uah=round(fx_spread_uah, 2),
            local_escrow_shipping_uah=round(local_escrow_uah, 2),
            base_landed_uah=round(base_landed_uah, 2),
            realistic_landed_uah=round(realistic_landed_uah, 2),
            billable_weight_kg=billable_weight_kg,
            risk_flags=risk_flags,
            notes_uk="; ".join(notes_parts),
        )

    def calculate_foreign_breakeven_price(
        self,
        best_ua_uah: float,
        route_country: str,
        currency: str,
        weight_kg: float = 1.5,
        volumetric_factor: float = 1.0,
        customs_mode: CustomsMode = CustomsMode.STANDARD_150EUR,
        direct_to_ua_export: bool = False,
        shipping_raw: float = 0.0,
    ) -> dict[str, Any]:
        """
        Binary search inverse of calculate() to find maximum item_price_raw in `currency`
        such that realistic_landed_uah <= best_ua_uah.
        """
        cur = currency.upper()
        if best_ua_uah <= 0:
            return {
                "max_foreign_price": 0.0,
                "currency": cur,
                "hint_uk": f"Має сенс шукати лише дешевше за 0.00 {cur} (бюджет UA <= 0 ₴)",
            }

        # Check if even at price=0 shipping+forwarder already exceeds best_ua_uah
        zero_res = self.calculate(
            item_price_raw=0.0,
            currency=cur,
            route_country=route_country,
            shipping_raw=shipping_raw,
            weight_kg=weight_kg,
            volumetric_factor=volumetric_factor,
            customs_mode=customs_mode,
            direct_to_ua_export=direct_to_ua_export,
        )
        if zero_res.realistic_landed_uah > best_ua_uah:
            return {
                "max_foreign_price": 0.0,
                "currency": cur,
                "hint_uk": (
                    f"Невигідно з {route_country}: навіть доставка ({round(zero_res.realistic_landed_uah)} ₴) "
                    f"дорожча за UA ({round(best_ua_uah)} ₴)"
                ),
            }

        low = 0.0
        # Generous upper bound accounting for VAT deduction + max budget
        rate = self.fx_rates_uah.get(cur, 1.0)
        high = (best_ua_uah / rate) * 2.0 + 100.0

        for _ in range(60):
            mid = (low + high) / 2.0
            res = self.calculate(
                item_price_raw=mid,
                currency=cur,
                route_country=route_country,
                shipping_raw=shipping_raw,
                weight_kg=weight_kg,
                volumetric_factor=volumetric_factor,
                customs_mode=customs_mode,
                direct_to_ua_export=direct_to_ua_export,
            )
            if res.realistic_landed_uah <= best_ua_uah:
                low = mid
            else:
                high = mid

        # Floor to cents (0.01) and verify inequality holds strictly after rounding
        candidate = int(low * 100) / 100.0
        while candidate > 0:
            check = self.calculate(
                item_price_raw=candidate,
                currency=cur,
                route_country=route_country,
                shipping_raw=shipping_raw,
                weight_kg=weight_kg,
                volumetric_factor=volumetric_factor,
                customs_mode=customs_mode,
                direct_to_ua_export=direct_to_ua_export,
            )
            if check.realistic_landed_uah <= best_ua_uah:
                break
            candidate = round(max(0.0, candidate - 0.01), 2)

        route_label = f"{route_country.upper()} Direct" if direct_to_ua_export else f"{route_country.upper()} форвардер"
        return {
            "max_foreign_price": round(candidate, 2),
            "currency": cur,
            "hint_uk": (
                f"Точка беззбитковості ({route_label}): до {candidate:.2f} {cur} "
                f"(еквівалент кращої ціни UA {round(best_ua_uah)} ₴ з урахуванням доставки та мита)"
            ),
        }
