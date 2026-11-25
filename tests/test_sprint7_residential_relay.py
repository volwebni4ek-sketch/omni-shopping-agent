from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omni_agent.cli import main as cli_main
from omni_agent.core.models import DealTierBadge
from omni_agent.storage.relay_ingest import ingest_relay_file

REPO_ROOT = Path(__file__).resolve().parent.parent
RELAY_SCRIPT_PATH = REPO_ROOT / "local_relay" / "omni_residential_harvester.py"
STARTER_TASKS_PATH = REPO_ROOT / "local_relay" / "relay_tasks.json"


def _load_relay_module():
    spec = importlib.util.spec_from_file_location("omni_residential_harvester", RELAY_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["omni_residential_harvester"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_relay_script_self_test_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, str(RELAY_SCRIPT_PATH), "--self-test"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    assert "--self-test PASSED" in proc.stdout


def test_starter_relay_tasks_json_valid_and_covers_3_channels() -> None:
    data = json.loads(STARTER_TASKS_PATH.read_text(encoding="utf-8"))
    tasks = data["tasks"]
    ids = {t["task_id"] for t in tasks}
    assert ids == {"TASK-MON-001", "TASK-AI-PC-002", "TASK-SMARTHOME-003"}

    by_id = {t["task_id"]: t for t in tasks}
    assert set(by_id["TASK-MON-001"]["channels"]) >= {"olx", "rozetka", "hotline", "prom"}
    assert by_id["TASK-AI-PC-002"]["min_ram_gb"] == 64
    assert by_id["TASK-AI-PC-002"]["require_local_pickup"] is True
    assert set(by_id["TASK-SMARTHOME-003"]["channels"]) >= {"aliexpress", "prom"}


def test_relay_parsers_olx_rozetka_prom_and_aliexpress_antibot(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = _load_relay_module()

    task = {
        "task_id": "T-PARSE",
        "raw_query": "LG 43UN700-B",
        "category": "monitors_large_format",
        "target_price_uah": 18500.0,
    }

    # 1. OLX HTML parser (direct /d/uk/obyavlenie/...html + location + condition)
    olx_html = """
    <html><body>
      <div data-cy="l-card">
        <a href="/d/uk/obyavlenie/lg-43un700-kyiv-IDAbc123.html"><h4>Монітор LG 43UN700-B 4K IPS</h4></a>
        <p>13 200 грн</p>
        <span>Вживане</span>
        <span>Київ, Оболонь - Сьогодні</span>
      </div>
      <div data-cy="l-card">
        <a href="/uk/list/q-lg-43/"><h4>Search link trap</h4></a>
        <p>12 000 грн</p>
      </div>
    </body></html>
    """
    olx_offers, olx_ml = relay.parse_olx_html(olx_html, task, "https://www.olx.ua/uk/list/ko/q-LG/")
    assert olx_ml is None
    assert len(olx_offers) == 1
    assert olx_offers[0]["url"] == "https://www.olx.ua/d/uk/obyavlenie/lg-43un700-kyiv-IDAbc123.html"
    assert olx_offers[0]["location"] == "Київ, Оболонь - Сьогодні"
    assert olx_offers[0]["raw_condition"] == "Вживане"
    assert olx_offers[0]["price_raw"] == 13200.0

    # 2. Rozetka 2-step hydration via mocked http_get
    def fake_rozetka_http(url: str, **_kwargs: Any) -> tuple[int, str, str]:
        if "search/api/v6" in url:
            return 200, json.dumps({"data": {"goods": [{"id": 389102002}, {"id": 389102999}]}}), url
        if "goodsId=389102002" in url:
            return 200, json.dumps({
                "data": {
                    "id": 389102002,
                    "title": 'Монітор 42.5" LG 43SQ700S-W 4K IPS',
                    "price": 18999,
                    "price_pcs": "18 999 ₴",
                    "href": "https://rozetka.com.ua/ua/lg-43sq700s-w/p389102002/",
                    "seller_id": 5,
                    "sell_status": "available",
                    "product": {"state": "new"},
                }
            }), url
        return 200, json.dumps({
            "data": {
                "id": 389102999,
                "title": 'Монітор 43" LG 43UN700-B Refurb',
                "price": 16500,
                "price_pcs": "16 500 ₴",
                "href": "/ua/lg-43un700-b/p389102999/",
                "seller_id": 9901,
                "sell_status": "available",
                "product": {"state": "refurbished"},
            }
        }), url

    monkeypatch.setattr(relay, "http_get", fake_rozetka_http)
    rz_offers, rz_ml = relay.harvest_rozetka(task, "LG 43UN700-B")
    assert rz_ml == []
    assert len(rz_offers) == 2
    by_oid = {o["offer_id"]: o for o in rz_offers}
    assert by_oid["rozetka_389102002"]["trust_tier"] == 1
    assert by_oid["rozetka_389102002"]["store_name"] == "Rozetka.ua"
    assert by_oid["rozetka_389102999"]["trust_tier"] == 2
    assert by_oid["rozetka_389102999"]["url"] == "https://rozetka.com.ua/ua/lg-43un700-b/p389102999/"

    # 3. Prom parser
    prom_html = """
    <div data-qaid="product_block">
      <a data-qaid="product_link" href="/ua/p9994301-lg-43sq700s-openbox.html">
        <span data-qaid="product_name">Монітор LG 43SQ700S-W Open Box</span>
      </a>
      <span data-qaid="product_price">15 400 ₴</span>
      <span data-qaid="company_name">OutletUA</span>
      <span data-qaid="product_presence">В наявності</span>
    </div>
    """
    prom_offers, prom_ml = relay.parse_prom_html(prom_html, task, "https://prom.ua/ua/search?search_term=LG")
    assert prom_ml is None
    assert len(prom_offers) == 1
    assert prom_offers[0]["url"] == "https://prom.ua/ua/p9994301-lg-43sq700s-openbox.html"
    assert prom_offers[0]["raw_condition"] == "Open Box"
    assert prom_offers[0]["price_raw"] == 15400.0

    # 4. AliExpress anti-bot fallback (x5sec -> MANUAL_CHECK_LINK)
    ali_x5sec = "<html><body><script>window.x5sec='block';</script></body></html>"
    ali_offers, ali_ml = relay.parse_aliexpress_html(
        ali_x5sec,
        task,
        "https://www.aliexpress.com/wholesale?SearchText=SLZB-06",
    )
    assert ali_offers == []
    assert ali_ml is not None
    assert ali_ml["reason_status"] == "ANTIBOT_CHALLENGE_PUNISH"
    assert "aliexpress.com/wholesale" in ali_ml["direct_search_url"]


@pytest.mark.asyncio
async def test_ingest_relay_file_end_to_end_with_guards_and_markdown(tmp_path: Path) -> None:
    relay = _load_relay_module()

    mock_bundle = {
        "schema_version": "1.0",
        "generated_at": "2026-11-25T16:45:00Z",
        "source_ip_mode": "residential_local",
        "tasks": [
            {
                "task_id": "TASK-AI-RELAY-1",
                "raw_query": "Mac Studio 128GB unified memory",
                "category": "ai_mini_pc",
                "allowed_conditions": ["brand_new", "open_box", "used_good", "refurbished"],
                "max_budget_uah": 200000.0,
                "target_price_uah": 145000.0,
                "estimated_weight_kg": 4.0,
                "pickup_regions": ["Київ", "Житомир"],
                "require_local_pickup": True,
                "min_ram_gb": 64,
                "channel_summary": {
                    "olx": {"status": "VERIFIED", "offers_count": 2},
                    "rozetka": {"status": "VERIFIED", "offers_count": 2},
                    "aliexpress": {"status": "MANUAL_CHECK_LINK", "offers_count": 0},
                },
                "offers": [
                    {
                        "offer_id": "olx_kyiv_128",
                        "channel": "olx",
                        "route_country": "UA",
                        "store_name": "OLX (Київ, Печерський - Сьогодні)",
                        "trust_tier": 2,
                        "title": "Apple Mac Studio M2 Ultra 128GB / 2TB",
                        "url": "https://www.olx.ua/d/uk/obyavlenie/mac-studio-kyiv-IDKyiv128.html",
                        "price_raw": 142000.0,
                        "currency": "UAH",
                        "raw_price_snippet": "142 000 грн",
                        "raw_condition": "Вживане",
                        "location": "Київ, Печерський - Сьогодні",
                    },
                    {
                        "offer_id": "olx_zt_64",
                        "channel": "olx",
                        "route_country": "UA",
                        "store_name": "OLX (Житомир - Вчора)",
                        "trust_tier": 2,
                        "title": "Apple Mac mini M4 Pro 64GB / 1TB",
                        "url": "https://www.olx.ua/d/uk/obyavlenie/mac-mini-zt-IDZhit64.html",
                        "price_raw": 84000.0,
                        "currency": "UAH",
                        "raw_price_snippet": "84 000 грн",
                        "raw_condition": "Вживане",
                        "location": "Житомир - Вчора",
                    },
                    {
                        "offer_id": "olx_lviv_out_of_geo",
                        "channel": "olx",
                        "route_country": "UA",
                        "store_name": "OLX (Львів - Сьогодні)",
                        "trust_tier": 2,
                        "title": "Apple Mac Studio M2 Ultra 128GB",
                        "url": "https://www.olx.ua/d/uk/obyavlenie/mac-studio-lviv-IDLviv128.html",
                        "price_raw": 135000.0,
                        "currency": "UAH",
                        "raw_price_snippet": "135 000 грн",
                        "raw_condition": "Вживане",
                        "location": "Львів - Сьогодні",
                    },
                    {
                        "offer_id": "rz_official_128",
                        "channel": "rozetka",
                        "route_country": "UA",
                        "store_name": "Rozetka.ua",
                        "trust_tier": 1,
                        "title": "Apple Mac Studio M2 Ultra 128GB / 2TB",
                        "url": "https://rozetka.com.ua/ua/apple_mac_studio/p579054886/",
                        "price_raw": 189999.0,
                        "currency": "UAH",
                        "raw_price_snippet": "189 999 ₴",
                        "raw_condition": "Нове",
                    },
                    {
                        "offer_id": "rz_search_url_trap",
                        "channel": "rozetka",
                        "route_country": "UA",
                        "store_name": "Rozetka.ua",
                        "trust_tier": 1,
                        "title": "Apple Mac Studio M2 Ultra 128GB",
                        "url": "https://rozetka.com.ua/ua/search/?text=mac+studio",
                        "price_raw": 160000.0,
                        "currency": "UAH",
                        "raw_price_snippet": "160 000 ₴",
                        "raw_condition": "Нове",
                    },
                    {
                        "offer_id": "rz_low_ram_24",
                        "channel": "rozetka",
                        "route_country": "UA",
                        "store_name": "Rozetka.ua",
                        "trust_tier": 1,
                        "title": "Apple Mac mini M4 24/1TB",
                        "url": "https://rozetka.com.ua/ua/apple_mac_mini/p555111/",
                        "price_raw": 49000.0,
                        "currency": "UAH",
                        "raw_price_snippet": "49 000 ₴",
                        "raw_condition": "Нове",
                    },
                ],
                "manual_links": [
                    {
                        "task_id": "TASK-AI-RELAY-1",
                        "channel": "aliexpress",
                        "route_country": "UA",
                        "reason_status": "ANTIBOT_CHALLENGE_PUNISH",
                        "currency": "USD",
                        "direct_search_url": "https://www.aliexpress.com/wholesale?SearchText=Mac+Studio",
                    }
                ],
            }
        ],
    }

    relay_json_path = tmp_path / "latest_relay_result.json"
    relay_json_path.write_text(json.dumps(mock_bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    # Verify COPY_TO_ANTIGRAVITY.md generation
    md_text = relay.build_copy_to_antigravity_markdown(mock_bundle)
    md_path = tmp_path / "COPY_TO_ANTIGRAVITY.md"
    md_path.write_text(md_text, encoding="utf-8")
    assert md_path.is_file()
    assert "TASK-AI-RELAY-1" in md_text
    assert "https://www.olx.ua/d/uk/obyavlenie/mac-studio-kyiv-IDKyiv128.html" in md_text
    assert "ANTIBOT_CHALLENGE_PUNISH" in md_text

    # Run ingestion pipeline end-to-end
    db_path = tmp_path / "relay_hub.db"
    reports_dir = tmp_path / "reports"
    results = await ingest_relay_file(
        relay_json_path,
        db_path=db_path,
        reports_dir=reports_dir,
        include_used=True,
    )
    assert len(results) == 1
    bundle = results[0]

    ranked_ids = {o["offer_id"] for o in bundle["ranked_offers"]}
    assert ranked_ids == {"olx_kyiv_128", "olx_zt_64", "rz_official_128"}

    # Verify direct URLs survived and traps/geo/low-RAM were pruned
    pruned_by_id = {o["offer_id"]: o for o in bundle["pruned_offers"]}
    assert set(pruned_by_id.keys()) == {"olx_lviv_out_of_geo", "rz_search_url_trap", "rz_low_ram_24"}
    assert pruned_by_id["rz_search_url_trap"]["deal_badge"] == DealTierBadge.PRUNED_OUTLIER.value
    assert "пошук або каталог" in pruned_by_id["rz_search_url_trap"]["prune_reason"]
    assert "радіусом" in pruned_by_id["olx_lviv_out_of_geo"]["prune_reason"]
    assert "мінімальну вимогу (64 ГБ)" in pruned_by_id["rz_low_ram_24"]["prune_reason"]

    # Manual link preserved with breakeven hint
    assert len(bundle["manual_links"]) == 1
    assert bundle["manual_links"][0]["channel"] == "aliexpress"

    # HTML & briefing exported
    assert Path(bundle["reports"]["latest_html"]).is_file()
    assert Path(bundle["reports"]["briefing_md"]).is_file()


def test_cli_ingest_relay_command_end_to_end(tmp_path: Path) -> None:
    mock_bundle = {
        "schema_version": "1.0",
        "generated_at": "2026-11-25T16:50:00Z",
        "source_ip_mode": "residential_local",
        "tasks": [
            {
                "task_id": "TASK-CLI-RELAY-1",
                "raw_query": 'Монітор 43" LG 43UN700-B',
                "category": "monitors_large_format",
                "allowed_conditions": ["brand_new", "open_box", "used_good"],
                "target_price_uah": 18500.0,
                "estimated_weight_kg": 11.5,
                "offers": [
                    {
                        "offer_id": "cli_rz_1",
                        "channel": "rozetka",
                        "route_country": "UA",
                        "store_name": "Rozetka.ua",
                        "trust_tier": 1,
                        "title": 'Монітор 43" LG 43UN700-B 4K IPS',
                        "url": "https://rozetka.com.ua/ua/lg-43un700-b/p389102099/",
                        "price_raw": 22500.0,
                        "currency": "UAH",
                        "raw_price_snippet": "22 500 ₴",
                        "raw_condition": "Нове",
                    },
                    {
                        "offer_id": "cli_olx_1",
                        "channel": "olx",
                        "route_country": "UA",
                        "store_name": "OLX (Київ - Сьогодні)",
                        "trust_tier": 2,
                        "title": 'Монітор 43" LG 43UN700-B 4K IPS б/в',
                        "url": "https://www.olx.ua/d/uk/obyavlenie/lg-43un700-IDCli1.html",
                        "price_raw": 13500.0,
                        "currency": "UAH",
                        "raw_price_snippet": "13 500 грн",
                        "raw_condition": "Вживане",
                        "location": "Київ - Сьогодні",
                    },
                ],
                "manual_links": [],
            }
        ],
    }
    relay_json_path = tmp_path / "latest_relay_result.json"
    relay_json_path.write_text(json.dumps(mock_bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    cli_db = tmp_path / "cli_relay.db"
    cli_reports = tmp_path / "cli_reports"
    rc = cli_main([
        "ingest-relay",
        str(relay_json_path),
        "--include-used",
        "--db-path",
        str(cli_db),
        "--export-dir",
        str(cli_reports),
    ])
    assert rc == 0
    assert (cli_reports / "latest_report.html").is_file()
    assert (cli_reports / "TASK-CLI-RELAY-1_briefing.md").is_file()
