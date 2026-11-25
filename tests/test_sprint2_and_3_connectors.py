from __future__ import annotations

import json
import httpx
import pytest

from omni_agent.connectors import (
    AliExpressConnector,
    AmazonEUConnector,
    CeneoPLConnector,
    HotlineConnector,
    IdealoDEConnector,
    OLXConnector,
    PromConnector,
    RozetkaConnector,
    SmartDeepLinkGenerator,
    TelegramPromoScraper,
    is_antibot_challenge,
    parse_telegram_channel_html,
)
from omni_agent.core.models import (
    ChannelTrustTier,
    ConditionCode,
    ShoppingTask,
    VerificationStatus,
)


def _sample_task(
    *,
    raw_query: str = "Dell UltraSharp U4323QE",
    mpns: list[str] | None = None,
    target_price_uah: float = 36000.0,
) -> ShoppingTask:
    return ShoppingTask(
        task_id="task_test_01",
        raw_query=raw_query,
        canonical_mpns=mpns if mpns is not None else ["U4323QE"],
        queries_i18n={
            "uk": raw_query,
            "en": raw_query,
            "de": raw_query,
            "pl": raw_query,
            "cz": raw_query,
        },
        estimated_weight_kg=5.0,
        target_price_uah=target_price_uah,
    )


def test_antibot_challenge_detector() -> None:
    assert is_antibot_challenge(403, "") is True
    assert is_antibot_challenge(429, "Too many requests") is True
    assert is_antibot_challenge(503, "Service Unavailable") is True
    assert is_antibot_challenge(200, "<html><div class='cf-turnstile'></div></html>") is True
    assert is_antibot_challenge(200, "<html>Please contact api-services-support@amazon.com</html>") is True
    assert is_antibot_challenge(200, "<html><script src='https://geo.captcha-delivery.com/datadome'></script></html>") is True
    assert is_antibot_challenge(200, "<html>_____tmd_____ x5sec punish</html>") is True
    assert is_antibot_challenge(200, "<html><body>Normal catalog page</body></html>") is False


def test_hotline_connector_mpn_discovery_and_price_range() -> None:
    html = """
    <html><body>
      <div class="list-item">
        <a class="item-title" href="/ua/computer-monitory/dell-ultrasharp-u4323qe/">Монітор Dell UltraSharp U4323QE 43" 4K</a>
        <div class="list-item__value-price">34 500 – 39 900 ₴</div>
        <div class="list-item__offers-count">14 пропозицій</div>
        <img src="//img.hotline.ua/u4323qe.jpg" />
      </div>
      <div class="list-item">
        <a class="item-title" href="/ua/computer-monitory/lg-43un700-b/">Монітор LG 43UN700-B 4K IPS</a>
        <div class="list-item__value-price">від 21 999 грн</div>
        <div class="list-item__offers-count">8 пропозицій</div>
      </div>
    </body></html>
    """
    task = _sample_task(raw_query="Монітор 43 4K", mpns=[])
    connector = HotlineConnector()
    res = connector.parse_html(html, task)

    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 2
    assert "U4323QE" in res.discovered_mpns
    assert "43UN700-B" in res.discovered_mpns

    first = res.offers[0]
    assert first.trust_tier == ChannelTrustTier.TIER_1_OFFICIAL
    assert first.warranty_months == 24
    assert first.delivery_days == 1
    assert first.route_country == "UA"
    assert first.price_raw == 34500.0
    assert first.specs["price_range_max_uah"] == 39900.0
    assert first.is_grounded is True
    assert first.landed_cost.realistic_landed_uah == 34500.0

    second = res.offers[1]
    assert second.price_raw == 21999.0


def test_prom_connector_card_parsing_and_condition_grounding() -> None:
    html = """
    <html><body>
      <div data-qaid="product_block">
        <a data-qaid="product_link" href="/p123456-delonghi-ecam.html">
          <span data-qaid="product_name">Кавомашина DeLonghi ECAM22.110.B Open Box вітринна</span>
        </a>
        <span data-qaid="product_price">14 299 ₴</span>
        <span data-qaid="company_name">EuroCoffee UA</span>
        <span data-qaid="company_rating">96%</span>
        <span data-qaid="product_presence">В наявності</span>
        <img src="https://images.prom.ua/ecam.jpg" />
      </div>
    </body></html>
    """
    task = _sample_task(raw_query="DeLonghi ECAM22.110.B", mpns=["ECAM22.110.B"])
    connector = PromConnector()
    res = connector.parse_html(html, task)

    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 1
    offer = res.offers[0]
    assert offer.offer_id == "prom_123456"
    assert offer.trust_tier == ChannelTrustTier.TIER_2_ESCROW
    assert offer.store_name == "EuroCoffee UA"
    assert offer.seller_rating == 4.8
    assert offer.price_raw == 14299.0
    assert offer.is_grounded is True
    assert offer.condition.code == ConditionCode.OPEN_BOX
    assert offer.landed_cost.realistic_landed_uah == 14299.0 + 70.0


def test_olx_connector_card_parsing_and_escrow_fee() -> None:
    html = """
    <html><body>
      <div data-cy="l-card">
        <a href="/d/uk/obyavlenie/delonghi-ecam22-110-b-IDXyZ99.html">
          <h4>Кавомашина DeLonghi ECAM22.110.B у хорошому стані, пробіг 1200 чашок</h4>
        </a>
        <p>11 500 грн</p>
        <span>Вживане</span>
        <span>Київ, Дарницький - Сьогодні о 10:15</span>
      </div>
      <div data-cy="l-card">
        <a href="/d/uk/obyavlenie/delonghi-parts-IDParts1.html">
          <h4>Кавомашина DeLonghi ECAM22.110.B не вмикається на запчастини</h4>
        </a>
        <p>2 500 грн</p>
        <span>Вживане</span>
      </div>
    </body></html>
    """
    task = _sample_task(raw_query="DeLonghi ECAM22.110.B", mpns=["ECAM22.110.B"])
    connector = OLXConnector()
    res = connector.parse_html(html, task)

    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 2

    good = res.offers[0]
    assert good.offer_id == "olx_XyZ99"
    assert good.trust_tier == ChannelTrustTier.TIER_2_ESCROW
    assert good.condition.code == ConditionCode.USED_GOOD
    assert good.condition.usage_counter == 1200
    assert good.landed_cost.local_escrow_shipping_uah > 120.0
    assert good.landed_cost.realistic_landed_uah > 11500.0

    parts = res.offers[1]
    assert parts.condition.code == ConditionCode.FOR_PARTS


async def test_olx_connector_aws_waf_fail_closed_manual_check_link() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, text="<html><script>AwsWafIntegration</script><div id='aws-waf'></div></html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task = _sample_task(raw_query="Dell U4323QE", target_price_uah=34000.0)
    connector = OLXConnector(use_headless_chrome=False, client=client)
    res = await connector.search(task, best_ua_uah=34000.0)

    assert res.status == VerificationStatus.MANUAL_CHECK_LINK
    assert res.offers == []
    assert res.manual_link is not None
    assert "olx.ua/uk/list/q-Dell-U4323QE/" in res.manual_link.direct_search_url
    assert res.manual_link.breakeven_max_foreign_price == 34000.0


def test_rozetka_connector_api_payload_and_fallback() -> None:
    payload = {
        "data": {
            "goods": [
                {
                    "id": 412345,
                    "title": "Монітор Dell UltraSharp U4323QE",
                    "price": 36999,
                    "price_pcs": "36 999 ₴",
                    "href": "/ua/dell_u4323qe/p412345/",
                    "image_main": "https://content.rozetka.com.ua/u4323qe.jpg",
                    "seller": "Rozetka",
                    "is_rozetka_seller": True,
                    "sell_status": "available",
                    "comments_mark": 4.9,
                    "comments_amount": 42,
                }
            ]
        }
    }
    task = _sample_task()
    connector = RozetkaConnector()
    res = connector.parse_api_payload(payload, task)

    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 1
    offer = res.offers[0]
    assert offer.offer_id == "rozetka_412345"
    assert offer.trust_tier == ChannelTrustTier.TIER_1_OFFICIAL
    assert offer.warranty_months == 24
    assert offer.seller_rating == 4.9
    assert offer.orders_or_reviews == 42


async def test_rozetka_connector_403_fail_closed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task = _sample_task()
    connector = RozetkaConnector(client=client)
    res = await connector.search(task, best_ua_uah=35000.0)

    assert res.status == VerificationStatus.MANUAL_CHECK_LINK
    assert res.manual_link is not None
    assert "seller=rozetka" in res.manual_link.direct_search_url


def test_aliexpress_init_data_variant_trap_and_telegram_coupon() -> None:
    tg_html = """
    <div class="tgme_widget_message_text">
      Гаряча знижка! Промокод: DEAL20 ($20 off $120)
      <a href="https://s.click.aliexpress.com/e/_demo">Купити</a>
    </div>
    """
    posts = parse_telegram_channel_html(tg_html, channel="ali_ua_deals")
    assert len(posts) == 1
    tg_coupons = posts[0].coupons
    assert tg_coupons[0].code == "DEAL20"
    assert tg_coupons[0].discount_amount == 20.0
    assert tg_coupons[0].min_spend == 120.0

    init_payload = {
        "mods": {
            "itemList": {
                "content": [
                    {
                        "productId": "100500111",
                        "title": {"displayTitle": "Baseus 140W GaN Charger DL24MP"},
                        "prices": {"salePrice": {"minPrice": 130.0, "formattedPrice": "$130.00", "currencyCode": "USD"}},
                        "evaluation": {"starRating": 4.9},
                        "trade": {"tradeDesc": "500+ sold"},
                        "store": {"storeName": "Baseus Official Store"},
                        "sellingPoints": [{"tagContent": {"tagText": "Free shipping"}}],
                    },
                    {
                        "productId": "100500222",
                        "title": {"displayTitle": "Baseus 140W Charger Replacement Cable Only"},
                        "prices": {"salePrice": {"minPrice": 8.0, "formattedPrice": "$8.00", "currencyCode": "USD"}},
                        "store": {"storeName": "Cable Shop"},
                    },
                    {
                        "productId": "100500333",
                        "title": {"displayTitle": "UGREEN 140W Power Station DL24MP"},
                        "prices": {"salePrice": {"minPrice": 140.0, "formattedPrice": "$140.00", "currencyCode": "USD"}},
                        "store": {"storeName": "UGREEN Choice Store"},
                    },
                ]
            }
        }
    }
    html = f"<html><script>window._init_data_={json.dumps(init_payload)};</script></html>"

    task = _sample_task(raw_query="140W GaN Charger", mpns=["DL24MP"], target_price_uah=6000.0)
    task.estimated_weight_kg = 0.6
    connector = AliExpressConnector(route_country="UA")
    res = connector.parse_html(html, task, extra_coupons=tg_coupons)

    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 3

    # Median of [130, 8, 140] is 130 -> 0.28 * 130 = 36.4 -> $8 cable is marked variant_trap=True!
    offers_by_id = {o.offer_id: o for o in res.offers}
    main_offer = offers_by_id["ali_ua_100500111"]
    cable_offer = offers_by_id["ali_ua_100500222"]

    assert main_offer.specs["variant_trap"] is False
    assert cable_offer.specs["variant_trap"] is True
    assert main_offer.landed_cost.applied_coupon_code == "DEAL20"
    assert main_offer.landed_cost.coupon_discount_raw == 20.0
    assert main_offer.trust_tier == ChannelTrustTier.TIER_1_OFFICIAL


def test_aliexpress_punish_x5sec_fail_closed() -> None:
    html = "<html><script>window.x5sec='abc';</script><div>punish captcha</div></html>"
    task = _sample_task(raw_query="Baseus 140W", target_price_uah=5000.0)
    task.estimated_weight_kg = 0.5
    connector = AliExpressConnector(route_country="UA")
    res = connector.parse_html(html, task, best_ua_uah=5000.0)

    assert res.status == VerificationStatus.MANUAL_CHECK_LINK
    assert res.offers == []
    assert res.manual_link is not None
    assert res.manual_link.currency == "USD"
    assert res.manual_link.breakeven_max_foreign_price is not None
    assert res.manual_link.breakeven_max_foreign_price > 0


async def test_aliexpress_multi_country_cookie_and_telegram_integration() -> None:
    captured_cookies: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if "t.me/s/" in str(request.url):
            return httpx.Response(
                200,
                text='<div class="tgme_widget_message_text">Код: PL15 ($15 off $100)</div>',
            )
        captured_cookies["cookie"] = request.headers.get("cookie", "")
        payload = {
            "mods": {
                "itemList": {
                    "content": [
                        {
                            "productId": "999888",
                            "title": {"displayTitle": "EcoFlow River 2 EU"},
                            "prices": {"salePrice": {"minPrice": 180.0, "formattedPrice": "€180.00", "currencyCode": "EUR"}},
                            "store": {"storeName": "EcoFlow Official"},
                        }
                    ]
                }
            }
        }
        return httpx.Response(200, text=f"<script>_init_data_={json.dumps(payload)};</script>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tg = TelegramPromoScraper(channels=["ali_deals"], client=client)
    connector = AliExpressConnector(route_country="DE", telegram_channels=["ali_deals"], telegram_scraper=tg, client=client)
    task = _sample_task(raw_query="EcoFlow River 2", mpns=[])
    res = await connector.search(task, route_country="DE")

    assert "region=DE" in captured_cookies["cookie"]
    assert "c_tp=EUR" in captured_cookies["cookie"]
    assert res.status == VerificationStatus.VERIFIED
    assert res.offers[0].route_country == "DE"
    assert res.offers[0].landed_cost.coupon_discount_raw > 0


def test_amazon_de_eur_warehouse_open_box_and_dual_route_vat() -> None:
    html = """
    <html><body>
      <div data-component-type="s-search-result" data-asin="B0BSHF7WHW">
        <h2><a href="/dp/B0BSHF7WHW">Dell UltraSharp U4323QE 42,5 Zoll 4K UHD USB-C Monitor</a></h2>
        <span class="a-price"><span class="a-offscreen">699,00 €</span></span>
        <span class="a-icon-alt">4,6 von 5 Sternen</span>
        <span class="a-size-base s-underline-text">184</span>
        <div data-cy="secondary-offer-recipe">
          <span>Gebraucht - Wie neu (Amazon Retourenkauf): 569,00 €</span>
        </div>
        <img class="s-image" src="https://m.media-amazon.com/images/I/u4323qe.jpg" />
      </div>
    </body></html>
    """
    task = _sample_task()
    connector = AmazonEUConnector(country="DE", direct_export_shipping_eur=25.0)
    res = connector.parse_html(html, task)

    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 2

    primary, warehouse = res.offers[0], res.offers[1]
    assert primary.currency == "EUR"
    assert primary.price_raw == 699.0
    assert primary.condition.code == ConditionCode.BRAND_NEW
    assert "dual_route_comparison" in primary.specs
    comp = primary.specs["dual_route_comparison"]
    assert comp["chosen_mode"] == "direct_export"
    assert comp["direct_export_uah"] < comp["forwarder_uah"]
    assert primary.landed_cost.direct_to_ua_export is True
    assert primary.landed_cost.vat_deducted_uah > 0

    assert warehouse.offer_id == "amz_de_B0BSHF7WHW_warehouse"
    assert warehouse.price_raw == 569.0
    assert warehouse.condition.code == ConditionCode.OPEN_BOX


def test_amazon_pl_pln_parsing() -> None:
    html = """
    <html><body>
      <div data-component-type="s-search-result" data-asin="B09TESTPLN">
        <h2><a href="/dp/B09TESTPLN">DeLonghi Magnifica S ECAM22.110.B Ekspres ciśnieniowy</a></h2>
        <span class="a-price"><span class="a-offscreen">1 399,00 zł</span></span>
        <span class="a-icon-alt">4,7 z 5 gwiazdek</span>
      </div>
    </body></html>
    """
    task = _sample_task(raw_query="DeLonghi ECAM22.110.B", mpns=["ECAM22.110.B"])
    connector = AmazonEUConnector(country="PL")
    res = connector.parse_html(html, task)

    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 1
    offer = res.offers[0]
    assert offer.route_country == "PL"
    assert offer.currency == "PLN"
    assert offer.price_raw == 1399.0
    assert offer.landed_cost.fx_spread_uah > 0
    assert offer.landed_cost.forwarder_fee_uah > 0


def test_amazon_captcha_fail_closed_with_eur_breakeven() -> None:
    captcha_html = """
    <html><head><title>Robot Check</title></head>
    <body>Sorry, we just need to make sure you're not a robot. api-services-support@amazon.com</body></html>
    """
    task = _sample_task(target_price_uah=35000.0)
    connector = AmazonEUConnector(country="DE")
    res = connector.parse_html(captcha_html, task, best_ua_uah=35000.0)

    assert res.status == VerificationStatus.MANUAL_CHECK_LINK
    assert res.offers == []
    assert res.manual_link is not None
    assert res.manual_link.currency == "EUR"
    assert res.manual_link.breakeven_max_foreign_price is not None
    assert res.manual_link.breakeven_max_foreign_price > 0
    assert "amazon.de/s?k=" in res.manual_link.direct_search_url


def test_idealo_de_connector_eur_and_kit_trap_and_cloudflare_fallback() -> None:
    html = """
    <html><body>
      <div class="sr-resultItemTile_B0odU">
        <a data-testid="product-title" href="/preisvergleich/OffersOfProduct/2023.html">Makita DHP484Z Solo ohne Akku</a>
        <div data-testid="price">ab 119,00 €</div>
        <div class="sr-detailedPriceInfo__offerCount">22 Angebote</div>
      </div>
    </body></html>
    """
    task = _sample_task(raw_query="Makita DHP484Z", mpns=["DHP484Z"])
    task.estimated_weight_kg = 2.0
    connector = IdealoDEConnector()
    res = connector.parse_html(html, task)

    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 1
    offer = res.offers[0]
    assert offer.currency == "EUR"
    assert offer.price_raw == 119.0
    assert offer.route_country == "DE"
    assert "Solo" in offer.specs.get("kit_trap", "")

    blocked = connector.parse_html("<div class='cf-turnstile'></div>", task, best_ua_uah=9000.0)
    assert blocked.status == VerificationStatus.MANUAL_CHECK_LINK
    assert blocked.manual_link is not None
    assert blocked.manual_link.currency == "EUR"
    assert "idealo.de" in blocked.manual_link.direct_search_url


def test_ceneo_pl_connector_pln_and_datadome_fallback() -> None:
    html = """
    <html><body>
      <div class="cat-prod-row" data-pid="154321">
        <strong class="cat-prod-row__name">
          <a href="/154321">Monitor Dell UltraSharp U4323QE 43 4K</a>
        </strong>
        <span class="price">od 2 899,00 zł</span>
        <span class="shop-numb">w 12 sklepach</span>
        <span class="product-score">4,8</span>
      </div>
    </body></html>
    """
    task = _sample_task(target_price_uah=36000.0)
    connector = CeneoPLConnector()
    res = connector.parse_html(html, task)

    assert res.status == VerificationStatus.VERIFIED
    assert len(res.offers) == 1
    offer = res.offers[0]
    assert offer.offer_id == "ceneo_pl_154321"
    assert offer.currency == "PLN"
    assert offer.price_raw == 2899.0
    assert offer.orders_or_reviews == 12
    assert offer.seller_rating == 4.8
    assert offer.landed_cost.fx_spread_uah > 0

    blocked = connector.parse_html("<script>datadome</script>", task, best_ua_uah=36000.0)
    assert blocked.status == VerificationStatus.MANUAL_CHECK_LINK
    assert blocked.manual_link is not None
    assert blocked.manual_link.currency == "PLN"
    assert blocked.manual_link.breakeven_max_foreign_price is not None


def test_smart_deep_link_generator_pl_cz_us() -> None:
    task = _sample_task(raw_query="Dell UltraSharp U4323QE", mpns=["U4323QE"])
    gen = SmartDeepLinkGenerator()
    links = gen.generate(task, best_ua_uah=36000.0)

    by_channel = {link.channel: link for link in links}
    assert set(by_channel.keys()) == {"allegro_pl", "alza_cz", "heureka_cz", "amazon_us", "ebay_us"}

    assert by_channel["allegro_pl"].currency == "PLN"
    assert by_channel["allegro_pl"].route_country == "PL"
    assert "allegro.pl/listing?string=U4323QE" in by_channel["allegro_pl"].direct_search_url
    assert (by_channel["allegro_pl"].breakeven_max_foreign_price or 0) > 0

    assert by_channel["alza_cz"].currency == "CZK"
    assert by_channel["alza_cz"].route_country == "CZ"
    assert "alza.cz/search.htm?exps=U4323QE" in by_channel["alza_cz"].direct_search_url

    assert by_channel["heureka_cz"].currency == "CZK"
    assert "heureka.cz" in by_channel["heureka_cz"].direct_search_url

    assert by_channel["amazon_us"].currency == "USD"
    assert by_channel["amazon_us"].route_country == "US"
    assert (by_channel["amazon_us"].breakeven_max_foreign_price or 0) > 0

    assert by_channel["ebay_us"].currency == "USD"
    assert "ebay.com/sch/i.html?_nkw=U4323QE" in by_channel["ebay_us"].direct_search_url
