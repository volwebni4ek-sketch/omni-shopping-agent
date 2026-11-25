from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Optional

from omni_agent.connectors.base import ManualCheckLink, build_manual_check_link
from omni_agent.core.compliance import ComplianceManager
from omni_agent.core.landed_cost import LandedCostEngine
from omni_agent.core.models import ShoppingTask


@dataclass(frozen=True)
class DeepLinkSpec:
    channel: str
    route_country: str
    currency: str
    url_template: str
    lang_key: str
    reason_status: str = "MANUAL_DEEPLINK_PARTNER"


DEEP_LINK_SPECS: tuple[DeepLinkSpec, ...] = (
    DeepLinkSpec(
        channel="allegro_pl",
        route_country="PL",
        currency="PLN",
        url_template="https://allegro.pl/listing?string={encoded}",
        lang_key="pl",
    ),
    DeepLinkSpec(
        channel="alza_cz",
        route_country="CZ",
        currency="CZK",
        url_template="https://www.alza.cz/search.htm?exps={encoded}",
        lang_key="cz",
    ),
    DeepLinkSpec(
        channel="heureka_cz",
        route_country="CZ",
        currency="CZK",
        url_template="https://www.heureka.cz/?h%5Bfraze%5D={encoded}",
        lang_key="cz",
    ),
    DeepLinkSpec(
        channel="amazon_us",
        route_country="US",
        currency="USD",
        url_template="https://www.amazon.com/s?k={encoded}",
        lang_key="en",
    ),
    DeepLinkSpec(
        channel="ebay_us",
        route_country="US",
        currency="USD",
        url_template="https://www.ebay.com/sch/i.html?_nkw={encoded}",
        lang_key="en",
    ),
)


UA_SECONDARY_DEEP_LINK_SPECS: tuple[DeepLinkSpec, ...] = (
    DeepLinkSpec(
        channel="olx_ua_used_kyiv",
        route_country="UA",
        currency="UAH",
        url_template="https://www.olx.ua/uk/list/q-{encoded}/?search%5Border%5D=filter_float_price:asc",
        lang_key="uk",
        reason_status="UA_LOCAL_INSPECTION_OLX",
    ),
    DeepLinkSpec(
        channel="prom_ua_used_outlet",
        route_country="UA",
        currency="UAH",
        url_template="https://prom.ua/ua/search?search_term={encoded}&condition=used",
        lang_key="uk",
        reason_status="UA_LOCAL_PROM_USED",
    ),
    DeepLinkSpec(
        channel="rozetka_ucinka",
        route_country="UA",
        currency="UAH",
        url_template="https://rozetka.com.ua/ua/search/?text={encoded}&section_id=80004&ucenka=1",
        lang_key="uk",
        reason_status="UA_LOCAL_ROZETKA_OUTLET",
    ),
    DeepLinkSpec(
        channel="telemart_refurb_used",
        route_country="UA",
        currency="UAH",
        url_template="https://telemart.ua/ua/search/?search_que={encoded}&markdown=1",
        lang_key="uk",
        reason_status="UA_LOCAL_TELEMART_REFURB",
    ),
    DeepLinkSpec(
        channel="compbest_eu_used",
        route_country="UA",
        currency="UAH",
        url_template="https://compbest.com.ua/ua/search/?search={encoded}",
        lang_key="uk",
        reason_status="UA_LOCAL_COMPBEST_USED_EU",
    ),
    DeepLinkSpec(
        channel="hotline_ua_direct",
        route_country="UA",
        currency="UAH",
        url_template="https://hotline.ua/ua/sr/?q={encoded}",
        lang_key="uk",
        reason_status="UA_LOCAL_HOTLINE_CATALOG",
    ),
    DeepLinkSpec(
        channel="izi_shafa_ua_used",
        route_country="UA",
        currency="UAH",
        url_template="https://izi.ua/uk/search?search_text={encoded}",
        lang_key="uk",
        reason_status="UA_LOCAL_IZI_USED",
    ),
)

UA_SECONDARY_CHANNEL_LABELS_UK: dict[str, str] = {
    "olx_ua_used_kyiv": "OLX Київ / Україна (б/в та нове — живий огляд пікселів)",
    "prom_ua_used_outlet": "Prom.ua (б/в та уцінка з Prom-оплатою)",
    "rozetka_ucinka": "Rozetka Уцінка / Вітрина (офіційна перевірка)",
    "telemart_refurb_used": "Telemart Factory Refurbished / Уцінка",
    "compbest_eu_used": "CompBest (перевірені б/в монітори та техніка з Європи)",
    "hotline_ua_direct": "Hotline.ua (порівняння всіх офіційних магазинів УА)",
    "izi_shafa_ua_used": "IZI / Shafa (вторинний ринок УА з безпечною оплатою)",
}


class SmartDeepLinkGenerator:
    """
    Generates structured ManualCheckLink objects with exact forwarder/customs
    breakeven foreign prices against best_ua_uah for PL/CZ/US marketplaces,
    plus Ukrainian Secondary / Refurbished / Open-Box inspection links.
    """

    def __init__(self, landed_cost_engine: Optional[LandedCostEngine] = None) -> None:
        self.landed_cost_engine = landed_cost_engine or LandedCostEngine()

    @staticmethod
    def _pick_query(task: ShoppingTask, lang_key: str) -> str:
        if task.canonical_mpns:
            raw = task.canonical_mpns[0]
        elif task.queries_i18n.get(lang_key):
            raw = task.queries_i18n[lang_key]
        elif lang_key == "uk" and task.raw_query:
            raw = task.raw_query
        elif task.queries_i18n.get("en"):
            raw = task.queries_i18n["en"]
        else:
            raw = task.raw_query
        return ComplianceManager.sanitize_gdpr_query(raw)

    def _build_from_specs(
        self,
        specs: tuple[DeepLinkSpec, ...],
        task: ShoppingTask,
        *,
        best_ua_uah: Optional[float] = None,
        channels: Optional[list[str]] = None,
        include_ua_labels: bool = False,
    ) -> list[ManualCheckLink]:
        links: list[ManualCheckLink] = []
        allowed = set(channels) if channels else None

        for spec in specs:
            if allowed is not None and spec.channel not in allowed:
                continue

            query = self._pick_query(task, spec.lang_key)
            if spec.channel == "olx_ua_used_kyiv":
                slug = "-".join(query.strip().split())
                encoded = urllib.parse.quote(slug)
            else:
                encoded = urllib.parse.quote_plus(query)
            direct_url = spec.url_template.format(encoded=encoded)

            link = build_manual_check_link(
                task=task,
                channel=spec.channel,
                route_country=spec.route_country,
                currency=spec.currency,
                direct_search_url=direct_url,
                reason_status=spec.reason_status,
                best_ua_uah=best_ua_uah,
                direct_to_ua_export=False,
                landed_cost_engine=self.landed_cost_engine,
            )
            if include_ua_labels and spec.channel in UA_SECONDARY_CHANNEL_LABELS_UK:
                label = UA_SECONDARY_CHANNEL_LABELS_UK[spec.channel]
                if link.breakeven_max_foreign_price:
                    link.breakeven_hint_uk = (
                        f"{label} — орієнтир вигідної ціни до {link.breakeven_max_foreign_price:.0f} ₴"
                    )
                else:
                    link.breakeven_hint_uk = label
            links.append(link)

        return links

    def generate(
        self,
        task: ShoppingTask,
        *,
        best_ua_uah: Optional[float] = None,
        channels: Optional[list[str]] = None,
    ) -> list[ManualCheckLink]:
        return self._build_from_specs(
            DEEP_LINK_SPECS,
            task,
            best_ua_uah=best_ua_uah,
            channels=channels,
            include_ua_labels=False,
        )

    def generate_ua_secondary_links(
        self,
        task: ShoppingTask,
        *,
        best_ua_uah: Optional[float] = None,
        channels: Optional[list[str]] = None,
    ) -> list[ManualCheckLink]:
        """Generate Ukrainian local used/open-box/refurbished/outlet inspection links."""
        return self._build_from_specs(
            UA_SECONDARY_DEEP_LINK_SPECS,
            task,
            best_ua_uah=best_ua_uah,
            channels=channels,
            include_ua_labels=True,
        )
