from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from omni_agent.cli import main as cli_main
from omni_agent.connectors.deep_links import SmartDeepLinkGenerator
from omni_agent.core.models import ConditionCode, CustomsMode, ShoppingTask
from omni_agent.orchestrator import OmniOrchestrator
from omni_agent.storage.drive_backup import backup_sqlite_to_drive
from omni_agent.storage.gsheets_bridge import (
    TOP_CHARTS_HEADERS_A_TO_V,
    GoogleSheetsBridge,
    export_master_sheets_payload,
)
from omni_agent.storage.sqlite_store import SQLiteStore
from omni_agent.ui.dashboard_app import create_app
from omni_agent.ui.report_renderer import render_standalone_hitl_html


@pytest.mark.asyncio
async def test_sqlite_store_persistence_price_drop_alerts_and_preserved_human_decisions(tmp_path: Path) -> None:
    db_path = tmp_path / "hub.db"
    reports_dir = tmp_path / "reports"
    orch = OmniOrchestrator(db_path=db_path, reports_dir=reports_dir, demo_mode=True)

    task = ShoppingTask(
        task_id="task_mon_persist",
        raw_query='Монітор 43" LG 43SQ700S-W 4K IPS',
        canonical_mpns=["43SQ700S-W"],
        category="monitors_large_format",
        target_price_uah=18500.0,
        estimated_weight_kg=11.5,
    )
    bundle1 = await orch.run_task(task, ua_only=True, include_used=True)
    assert bundle1["ranked_offers"]

    target_offer_id = bundle1["ranked_offers"][0]["offer_id"]
    orch.store.update_human_decision(
        target_offer_id,
        decision="APPROVE",
        target_price_uah=18000.0,
        notes="Перевірити биті пікселі при отриманні",
    )

    # Simulate price drop on second harvest for the same offer_id
    re_offers, re_links = orch._generate_demo_harvest(
        orch._prepare_task(task, include_used=True),
        ua_only=True,
        include_used=True,
    )
    for o in re_offers:
        if o.offer_id == target_offer_id:
            o.landed_cost.realistic_landed_uah = round(o.landed_cost.realistic_landed_uah - 1200.0, 2)

    bundle2 = await orch.run_task(
        task,
        ua_only=True,
        include_used=True,
        injected_offers=re_offers,
        injected_manual_links=re_links,
    )

    # Human decision must survive re-harvest!
    matched = [o for o in bundle2["offers"] if o["offer_id"] == target_offer_id][0]
    assert matched["human_decision"] == "APPROVE"
    assert matched["human_target_price_uah"] == 18000.0
    assert "пікселі" in matched["human_notes"]

    # Price drop alert must trigger
    assert any(
        a["offer_id"] == target_offer_id and "PRICE_DROP" in a["alert_type"]
        for a in bundle2["alerts"]
    )

    # Historical snapshots accumulated (>= 2 snapshots for target_offer_id)
    with sqlite3.connect(db_path) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM price_history_snapshots WHERE offer_id = ?",
            (target_offer_id,),
        ).fetchone()[0]
    assert cnt >= 2
    orch.store.close()


def test_drive_backup_creates_atomic_valid_sqlite_copy(tmp_path: Path) -> None:
    db_path = tmp_path / "shopping_hub.db"
    store = SQLiteStore(db_path)
    store.upsert_task(ShoppingTask(task_id="t_bak", raw_query="LG 43UN700-B"))
    store.close()

    backup_dir = tmp_path / "gdrive_backups"
    dest = backup_sqlite_to_drive(db_path, backup_dir=str(backup_dir))
    assert dest is not None
    assert dest.exists()
    assert dest.parent == backup_dir

    with sqlite3.connect(dest) as conn:
        rows = conn.execute("SELECT task_id FROM tasks").fetchall()
    assert rows == [("t_bak",)]


@pytest.mark.asyncio
async def test_gsheets_payload_22_columns_and_bridge_http_sync(tmp_path: Path) -> None:
    orch = OmniOrchestrator(db_path=tmp_path / "db.sqlite", reports_dir=tmp_path / "rep", demo_mode=True)
    task = orch.build_task_from_query('LG 43SQ700S-W 43" 4K IPS', include_used=True)
    bundle = await orch.run_task(task, ua_only=False, include_used=True)

    payload = export_master_sheets_payload(bundle)
    top_tab = payload["2_Top_Charts_And_Jackpots"]
    assert len(TOP_CHARTS_HEADERS_A_TO_V) == 22
    assert top_tab["headers_a_to_v"] == TOP_CHARTS_HEADERS_A_TO_V
    assert top_tab["preserved_human_headers_w_to_y"] == ["Human_Decision", "Target_Price_UAH", "Human_Notes"]
    assert len(top_tab["rows"]) >= 3
    for row in top_tab["rows"]:
        assert len(row) == 22

    assert len(payload["3_Manual_Check_Links"]["rows"]) >= 5
    assert len(payload["4_Funnel_Audit_And_Quarantine"]["rows"]) >= 2

    # Mock Google Apps Script Web App
    seen_requests: list[tuple[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "tasks": [
                        {
                            "task_id": "sheet_task_1",
                            "raw_query": "LG 43UN700-B",
                            "category": "monitors_large_format",
                            "allowed_conditions": ["brand_new", "used_good"],
                            "status": "PENDING",
                        }
                    ],
                },
            )
        body = json.loads(request.content.decode("utf-8"))
        assert body["action"] == "push_harvest_results"
        assert body["secret"] == "sec123"
        assert len(body["payload"]["2_Top_Charts_And_Jackpots"]["rows"][0]) == 22
        return httpx.Response(200, json={"ok": True, "top_rows_written": len(body["payload"]["2_Top_Charts_And_Jackpots"]["rows"])})

    transport = httpx.MockTransport(_handler)
    bridge = GoogleSheetsBridge(
        webapp_url="https://script.google.com/macros/s/demo/exec",
        sync_secret="sec123",
        transport=transport,
    )
    pulled = await bridge.pull_pending_tasks()
    assert len(pulled) == 1
    assert pulled[0].task_id == "sheet_task_1"

    pushed = await bridge.push_harvest_results(bundle)
    assert pushed["ok"] is True
    assert pushed["top_rows_written"] == len(top_tab["rows"])
    orch.store.close()


def test_ua_secondary_links_include_olx_prom_rozetka_telemart_compbest_hotline() -> None:
    task = ShoppingTask(
        task_id="t_ua_links",
        raw_query='Монітор 43" LG 43SQ700S-W',
        canonical_mpns=["43SQ700S-W"],
        target_price_uah=18500.0,
    )
    gen = SmartDeepLinkGenerator()
    ua_links = gen.generate_ua_secondary_links(task, best_ua_uah=18999.0)
    by_ch = {l.channel: l for l in ua_links}

    assert {
        "olx_ua_used_kyiv",
        "prom_ua_used_outlet",
        "rozetka_ucinka",
        "telemart_refurb_used",
        "compbest_eu_used",
        "hotline_ua_direct",
        "izi_shafa_ua_used",
    }.issubset(by_ch.keys())
    assert "olx.ua" in by_ch["olx_ua_used_kyiv"].direct_search_url
    assert "telemart.ua" in by_ch["telemart_refurb_used"].direct_search_url
    assert "compbest.com.ua" in by_ch["compbest_eu_used"].direct_search_url
    assert all(l.route_country == "UA" and l.currency == "UAH" for l in ua_links)


@pytest.mark.asyncio
async def test_orchestrator_ua_only_include_used_lg_ips_vs_samsung_va_monitors(tmp_path: Path) -> None:
    orch = OmniOrchestrator(db_path=tmp_path / "ua.db", reports_dir=tmp_path / "rep", demo_mode=True)
    task = orch.build_task_from_query(
        'Монітор 43" 4K IPS LG 43SQ700S-W / 43UN700-B для роботи з текстом і кіно',
        category="monitors_large_format",
        max_budget_uah=25000.0,
        estimated_weight_kg=11.5,
        include_used=True,
    )
    bundle = await orch.run_task(task, ua_only=True, include_used=True)

    # All ranked offers & manual links must be strictly UA in --ua-only mode
    assert all(o["route_country"] == "UA" for o in bundle["ranked_offers"])
    assert all(l["route_country"] == "UA" for l in bundle["manual_links"])

    # Both LG IPS (new + used/open-box) and Samsung VA offers are evaluated, and LG IPS outscores Samsung VA
    titles = [o["title"] for o in bundle["ranked_offers"]]
    assert any("LG 43SQ700S-W" in t for t in titles)
    assert any("LG 43UN700-B" in t and "б/в" in t for t in titles)
    assert any("Samsung" in t and "M7" in t for t in titles)

    lg_new = [o for o in bundle["ranked_offers"] if "LG 43SQ700S-W" in o["title"] and o["condition"]["code"] == "brand_new"][0]
    samsung_new = [o for o in bundle["ranked_offers"] if "Samsung" in o["title"] and o["condition"]["code"] == "brand_new"][0]
    assert lg_new["quality_score"] > samsung_new["quality_score"]

    # Accessory & broken matrix traps must be pruned in Stage 1
    pruned_titles = [p["title"] for p in bundle["pruned_offers"]]
    assert any("Кронштейн" in t for t in pruned_titles)
    assert any("бита матриця" in t for t in pruned_titles)

    # Reports exist
    assert Path(bundle["reports"]["latest_html"]).exists()
    assert Path(bundle["reports"]["briefing_md"]).exists()
    orch.store.close()


@pytest.mark.asyncio
async def test_orchestrator_global_mode_and_power_station_energy_exempt(tmp_path: Path) -> None:
    orch = OmniOrchestrator(db_path=tmp_path / "global.db", reports_dir=tmp_path / "rep", demo_mode=True)
    task = orch.build_task_from_query(
        "EcoFlow DELTA 2 Max 2048Wh LiFePO4",
        category="power_stations",
        estimated_weight_kg=23.0,
        include_used=True,
    )
    assert task.customs_mode == CustomsMode.ENERGY_EXEMPT
    bundle = await orch.run_task(task, ua_only=False, include_used=True)

    routes = {o["route_country"] for o in bundle["ranked_offers"]}
    assert "UA" in routes and "DE" in routes
    de_offer = [o for o in bundle["ranked_offers"] if o["route_country"] == "DE"][0]
    assert de_offer["landed_cost"]["customs_duty_vat_uah"] == 0.0
    assert de_offer["landed_cost"]["vat_deducted_uah"] > 0.0

    # Global deep links present alongside UA secondary links
    link_countries = {l["route_country"] for l in bundle["manual_links"]}
    assert {"UA", "PL", "CZ", "US"}.issubset(link_countries)
    orch.store.close()


@pytest.mark.asyncio
async def test_render_standalone_hitl_html_contains_all_4_zones_and_sliders(tmp_path: Path) -> None:
    orch = OmniOrchestrator(db_path=tmp_path / "html.db", reports_dir=tmp_path / "rep", demo_mode=True)
    task = orch.build_task_from_query('Монітор 43" LG 43SQ700S-W', include_used=True)
    bundle = await orch.run_task(task, ua_only=True, include_used=True)

    html = render_standalone_hitl_html(bundle)
    assert "Zone A" in html
    assert "Zone B" in html
    assert "Zone C" in html
    assert "Zone D" in html
    assert "recomputeTopsis()" in html
    assert "updateCalc()" in html
    assert "olx_ua_used_kyiv" in html
    assert "telemart_refurb_used" in html
    assert "LG 43SQ700S-W" in html
    orch.store.close()


def test_cli_search_and_run_queue_commands(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.db"
    html_out = tmp_path / "out" / "custom_report.html"
    sheets_out = tmp_path / "out" / "sheets.json"

    rc1 = cli_main(
        [
            "search",
            'LG 43SQ700S-W 43" 4K IPS',
            "--ua-only",
            "--include-used",
            "--demo",
            "--budget",
            "25000",
            "--weight-kg",
            "11.5",
            "--db-path",
            str(db_path),
            "--export-html",
            str(html_out),
            "--export-sheets-json",
            str(sheets_out),
        ]
    )
    assert rc1 == 0
    assert html_out.exists()
    assert sheets_out.exists()
    sheets_data = json.loads(sheets_out.read_text(encoding="utf-8"))
    assert "2_Top_Charts_And_Jackpots" in sheets_data

    tasks_json = tmp_path / "tasks.json"
    tasks_json.write_text(
        json.dumps(
            [
                {
                    "task_id": "queued_lg_43",
                    "raw_query": "LG 43UN700-B 43 4K IPS",
                    "category": "monitors_large_format",
                    "allowed_conditions": ["brand_new"],
                    "estimated_weight_kg": 11.5,
                }
            ]
        ),
        encoding="utf-8",
    )
    rep_dir = tmp_path / "queue_reports"
    rc2 = cli_main(
        [
            "run-queue",
            "--tasks-json",
            str(tasks_json),
            "--ua-only",
            "--include-used",
            "--demo",
            "--db-path",
            str(db_path),
            "--export-dir",
            str(rep_dir),
        ]
    )
    assert rc2 == 0
    assert (rep_dir / "latest_report.html").exists()
    assert (rep_dir / "queued_lg_43_briefing.md").exists()


def test_fastapi_dashboard_endpoints_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "api.db"
    rep_dir = tmp_path / "api_reports"
    app = create_app(db_path=db_path, reports_dir=rep_dir)
    client = TestClient(app)

    # Empty state renders onboarding HTML
    r0 = client.get("/")
    assert r0.status_code == 200
    assert "Omni Shopping Agent" in r0.text

    # POST /api/search creates & runs task in ua_only + include_used demo mode
    r_search = client.post(
        "/api/search",
        json={
            "query": 'LG 43SQ700S-W 43" 4K IPS',
            "category": "monitors_large_format",
            "max_budget_uah": 25000,
            "estimated_weight_kg": 11.5,
            "ua_only": True,
            "include_used": True,
            "demo": True,
        },
    )
    assert r_search.status_code == 200
    data = r_search.json()
    task_id = data["task_id"]
    first_offer_id = data["ranked_offers"][0]["offer_id"]

    # GET /api/tasks
    r_tasks = client.get("/api/tasks")
    assert r_tasks.status_code == 200
    assert any(t["task_id"] == task_id for t in r_tasks.json())

    # POST /api/decisions
    r_dec = client.post(
        "/api/decisions",
        json={
            "offer_id": first_offer_id,
            "decision": "APPROVE",
            "target_price_uah": 18200.0,
            "notes": "Купити після перевірки пікселів",
        },
    )
    assert r_dec.status_code == 200
    assert r_dec.json()["decision"]["decision"] == "APPROVE"

    # GET /api/tasks/{task_id} reflects persisted decision
    r_bundle = client.get(f"/api/tasks/{task_id}")
    assert r_bundle.status_code == 200
    matched = [o for o in r_bundle.json()["offers"] if o["offer_id"] == first_offer_id][0]
    assert matched["human_decision"] == "APPROVE"

    # GET / renders full report with the task
    r_home = client.get(f"/?task_id={task_id}")
    assert r_home.status_code == 200
    assert "LG 43SQ700S-W" in r_home.text
    assert "APPROVE" in r_home.text


def test_prepare_task_include_used_expands_allowed_conditions(tmp_path: Path) -> None:
    orch = OmniOrchestrator(db_path=tmp_path / "cond.db", reports_dir=tmp_path / "rep", demo_mode=True)
    base = ShoppingTask(task_id="t_c", raw_query="LG 43UN700-B")
    assert base.allowed_conditions == [ConditionCode.BRAND_NEW, ConditionCode.OPEN_BOX]
    expanded = orch._prepare_task(base, include_used=True)
    assert ConditionCode.USED_GOOD in expanded.allowed_conditions
    assert ConditionCode.REFURBISHED in expanded.allowed_conditions
    orch.store.close()
