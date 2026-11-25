"""Multi-channel UA, AliExpress, EU, and Global Deep-Link connectors."""

from omni_agent.connectors.aliexpress import (
    SUPPORTED_ALIEXPRESS_ROUTES,
    AliExpressAntiBotError,
    AliExpressConnector,
    AliExpressParseError,
    AliExpressRawDeal,
    extract_init_data_json,
    parse_search_items,
)
from omni_agent.connectors.amazon_eu import AMAZON_EU_DOMAINS, AmazonEUConnector
from omni_agent.connectors.base import (
    BaseConnector,
    ChannelHarvestResult,
    ManualCheckLink,
    build_browser_headers,
    build_manual_check_link,
    is_antibot_challenge,
    parse_price_range,
    parse_price_value,
    polite_get,
)
from omni_agent.connectors.ceneo_pl import CeneoPLConnector
from omni_agent.connectors.deep_links import (
    DEEP_LINK_SPECS,
    UA_SECONDARY_CHANNEL_LABELS_UK,
    UA_SECONDARY_DEEP_LINK_SPECS,
    SmartDeepLinkGenerator,
)
from omni_agent.connectors.hotline import HotlineConnector
from omni_agent.connectors.idealo_de import IdealoDEConnector
from omni_agent.connectors.olx import OLXConnector
from omni_agent.connectors.prom import PromConnector
from omni_agent.connectors.rozetka_smart import RozetkaConnector
from omni_agent.connectors.telegram_promos import (
    TelegramDealPost,
    TelegramPromoScraper,
    parse_telegram_channel_html,
)

__all__ = [
    "AMAZON_EU_DOMAINS",
    "AliExpressAntiBotError",
    "AliExpressConnector",
    "AliExpressParseError",
    "AliExpressRawDeal",
    "AmazonEUConnector",
    "BaseConnector",
    "CeneoPLConnector",
    "ChannelHarvestResult",
    "DEEP_LINK_SPECS",
    "HotlineConnector",
    "IdealoDEConnector",
    "ManualCheckLink",
    "OLXConnector",
    "PromConnector",
    "RozetkaConnector",
    "SUPPORTED_ALIEXPRESS_ROUTES",
    "SmartDeepLinkGenerator",
    "TelegramDealPost",
    "TelegramPromoScraper",
    "UA_SECONDARY_CHANNEL_LABELS_UK",
    "UA_SECONDARY_DEEP_LINK_SPECS",
    "build_browser_headers",
    "build_manual_check_link",
    "extract_init_data_json",
    "is_antibot_challenge",
    "parse_price_range",
    "parse_price_value",
    "parse_search_items",
    "parse_telegram_channel_html",
    "polite_get",
]
