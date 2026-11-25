from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Optional, Sequence

import uvicorn

from omni_agent.core.models import CustomsMode, ShoppingTask
from omni_agent.orchestrator import OmniOrchestrator
from omni_agent.storage.gsheets_bridge import GoogleSheetsBridge
from omni_agent.storage.relay_ingest import ingest_relay_file


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omni-agent",
        description="Omni Shopping Agent — UA/EU/Global Shopping Intelligence & HITL Portal",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. search
    p_search = sub.add_parser("search", help="Run single-query multi-channel search")
    p_search.add_argument("query", help="Product search query")
    p_search.add_argument("--ua-only", action="store_true", help="Focus strictly on Ukrainian channels + UA secondary/used links")
    p_search.add_argument("--include-used", action="store_true", help="Include USED_GOOD, OPEN_BOX, and REFURBISHED offers")
    p_search.add_argument("--category", default="general", help="Category profile (monitors_large_format, power_stations, general)")
    p_search.add_argument("--budget", type=float, default=None, help="Max budget in UAH")
    p_search.add_argument("--target-price", type=float, default=None, help="Target price alert threshold in UAH")
    p_search.add_argument("--weight-kg", type=float, default=1.5, help="Estimated shipping weight in kg")
    p_search.add_argument("--customs-mode", choices=["standard_150eur", "energy_exempt", "none"], default=None)
    p_search.add_argument("--demo", action="store_true", help="Use offline realistic multi-channel demo offers")
    p_search.add_argument("--db-path", default="data/shopping_hub.db", help="SQLite DB path")
    p_search.add_argument("--export-html", default=None, help="Path for standalone HTML report")
    p_search.add_argument("--export-sheets-json", default=None, help="Path to write Google Sheets 2D payload JSON")
    p_search.add_argument("--backup-drive", action="store_true", help="Backup SQLite DB to Google Drive folder if configured")

    # 2. run-queue
    p_queue = sub.add_parser("run-queue", help="Execute a queue of ShoppingTasks from JSON, SQLite, or Google Sheets")
    p_queue.add_argument("--ua-only", action="store_true", help="Focus strictly on Ukrainian channels + UA secondary/used links")
    p_queue.add_argument("--include-used", action="store_true", help="Include USED_GOOD, OPEN_BOX, and REFURBISHED offers")
    p_queue.add_argument("--tasks-json", default=None, help="Path to JSON file containing a list of ShoppingTask dicts")
    p_queue.add_argument("--from-gsheets", action="store_true", help="Pull pending tasks from Google Sheets Web App")
    p_queue.add_argument("--push-gsheets", action="store_true", help="Push results back to Google Sheets Web App")
    p_queue.add_argument("--export-dir", default="reports", help="Directory for HTML & Markdown reports")
    p_queue.add_argument("--db-path", default="data/shopping_hub.db", help="SQLite DB path")
    p_queue.add_argument("--demo", action="store_true", help="Use offline realistic demo offers")
    p_queue.add_argument("--backup-drive", action="store_true", help="Backup SQLite DB to Google Drive folder if configured")

    # 3. serve
    p_serve = sub.add_parser("serve", help="Start local FastAPI HITL Portal")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host")
    p_serve.add_argument("--port", type=int, default=8765, help="Bind port")
    p_serve.add_argument("--db-path", default="data/shopping_hub.db", help="SQLite DB path")
    p_serve.add_argument("--reports-dir", default="reports", help="Reports directory")

    # 4. ingest-relay
    p_relay = sub.add_parser(
        "ingest-relay",
        help="Ingest a local residential relay JSON result into SQLite, IQR funnel, TOPSIS & HTML report",
    )
    p_relay.add_argument("relay_json", help="Path to latest_relay_result.json or relay_result_*.json")
    p_relay.add_argument("--ua-only", action="store_true", help="Filter ingested offers strictly to UA channels")
    p_relay.add_argument("--include-used", action="store_true", help="Include USED_GOOD, OPEN_BOX, and REFURBISHED offers")
    p_relay.add_argument("--export-dir", default="reports", help="Directory for HTML & Markdown reports")
    p_relay.add_argument("--db-path", default="data/shopping_hub.db", help="SQLite DB path")
    p_relay.add_argument("--push-gsheets", action="store_true", help="Push results back to Google Sheets Web App")
    p_relay.add_argument("--backup-drive", action="store_true", help="Backup SQLite DB to Google Drive folder if configured")

    return parser


def _default_demo_queue() -> list[ShoppingTask]:
    return [
        OmniOrchestrator.build_task_from_query(
            'Монітор 43" 4K IPS LG 43SQ700S-W / 43UN700-B для роботи з текстом і кіно',
            category="monitors_large_format",
            max_budget_uah=25000.0,
            target_price_uah=18500.0,
            estimated_weight_kg=11.5,
            include_used=True,
        ),
    ]


async def _run_search_cmd(args: argparse.Namespace) -> dict[str, Any]:
    reports_dir = Path(args.export_html).parent if args.export_html else Path("reports")
    orch = OmniOrchestrator(
        db_path=args.db_path,
        reports_dir=reports_dir,
        demo_mode=args.demo,
    )
    customs_mode = CustomsMode(args.customs_mode) if args.customs_mode else None
    task = orch.build_task_from_query(
        args.query,
        category=args.category,
        max_budget_uah=args.budget,
        target_price_uah=args.target_price,
        estimated_weight_kg=args.weight_kg,
        include_used=args.include_used,
        customs_mode=customs_mode,
    )
    bundle = await orch.run_task(
        task,
        ua_only=args.ua_only,
        include_used=args.include_used,
        demo_mode=args.demo,
        export_html_path=args.export_html,
        backup_to_drive=args.backup_drive,
    )
    if args.export_sheets_json:
        out_path = Path(args.export_sheets_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(bundle["sheets_payload"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return bundle


async def _run_queue_cmd(args: argparse.Namespace) -> list[dict[str, Any]]:
    orch = OmniOrchestrator(
        db_path=args.db_path,
        reports_dir=args.export_dir,
        demo_mode=args.demo,
    )
    tasks: list[ShoppingTask] = []

    if args.tasks_json:
        raw_data = json.loads(Path(args.tasks_json).read_text(encoding="utf-8"))
        if isinstance(raw_data, dict):
            raw_data = raw_data.get("tasks") or [raw_data]
        tasks.extend(ShoppingTask.model_validate(item) for item in raw_data)

    if args.from_gsheets:
        bridge = GoogleSheetsBridge()
        if bridge.enabled:
            tasks.extend(await bridge.pull_pending_tasks())

    if not tasks:
        pending = orch.store.list_tasks(status="PENDING")
        tasks.extend(pending)

    if not tasks:
        tasks = _default_demo_queue()

    bundles: list[dict[str, Any]] = []
    for t in tasks:
        b = await orch.run_task(
            t,
            ua_only=args.ua_only,
            include_used=args.include_used,
            demo_mode=args.demo,
            push_to_gsheets=args.push_gsheets,
            backup_to_drive=args.backup_drive,
        )
        bundles.append(b)
    return bundles


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "search":
        bundle = asyncio.run(_run_search_cmd(args))
        ranked = bundle.get("ranked_offers") or []
        print(f"[omni-agent] Task: {bundle['task_id']} | Valid ranked offers: {len(ranked)}")
        if ranked:
            top = ranked[0]
            print(
                f"[omni-agent] Top #1: {top['title']} — {round(top['landed_cost']['realistic_landed_uah'])} UAH "
                f"({top['channel']}, TOPSIS={top['topsis_score']:.3f})"
            )
        print(f"[omni-agent] HTML report: {bundle['reports']['latest_html']}")
        return 0

    if args.command == "run-queue":
        bundles = asyncio.run(_run_queue_cmd(args))
        print(f"[omni-agent] Processed {len(bundles)} queued task(s).")
        for b in bundles:
            print(
                f"  - {b['task_id']}: {len(b.get('ranked_offers') or [])} ranked | "
                f"HTML -> {b['reports']['latest_html']}"
            )
        return 0

    if args.command == "serve":
        from omni_agent.ui.dashboard_app import create_app

        app = create_app(db_path=args.db_path, reports_dir=args.reports_dir)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    if args.command == "ingest-relay":
        bundles = asyncio.run(
            ingest_relay_file(
                args.relay_json,
                db_path=args.db_path,
                reports_dir=args.export_dir,
                ua_only=args.ua_only,
                include_used=args.include_used,
                push_to_gsheets=args.push_gsheets,
                backup_to_drive=args.backup_drive,
            )
        )
        print(f"[omni-agent] Ingested {len(bundles)} relay task(s) from {args.relay_json}.")
        for b in bundles:
            ranked = b.get("ranked_offers") or []
            print(
                f"  - {b['task_id']}: {len(ranked)} ranked | "
                f"HTML -> {b['reports']['latest_html']}"
            )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
