from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from omni_agent.core.models import ShoppingTask

# Exact 22 columns (A..V) written by the harvester.
# Human columns W..Y (Human_Decision, Target_Price_UAH, Human_Notes) are strictly preserved.
TOP_CHARTS_HEADERS_A_TO_V: list[str] = [
    "task_id",
    "offer_id",
    "rank",
    "hero_slot",
    "deal_badge",
    "channel",
    "route_country",
    "store_name",
    "trust_tier",
    "title",
    "mpn_codes",
    "condition_code",
    "condition_grade",
    "price_raw",
    "currency",
    "realistic_landed_uah",
    "discount_vs_median_pct",
    "effective_quality_score",
    "channel_score",
    "topsis_score",
    "tradeoff_summary_uk",
    "url",
]

MANUAL_LINKS_HEADERS: list[str] = [
    "task_id",
    "channel",
    "route_country",
    "reason_status",
    "breakeven_max_foreign_price",
    "currency",
    "breakeven_hint_uk",
    "direct_search_url",
]

FUNNEL_AUDIT_HEADERS: list[str] = [
    "task_id",
    "row_type",
    "offer_id",
    "deal_badge",
    "channel",
    "route_country",
    "title",
    "realistic_landed_uah",
    "reason_or_summary",
    "url",
]


def _as_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return obj
    return {}


def export_master_sheets_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    """
    Converts a task bundle (from SQLiteStore.get_task_bundle or OmniOrchestrator.run_task)
    into exact 2D row arrays for Google Sheets tabs:
      - 2_Top_Charts_And_Jackpots (exact 22 columns A..V; preserves human columns W..Y)
      - 3_Manual_Check_Links
      - 4_Funnel_Audit_And_Quarantine
    """
    task_id = str(bundle.get("task_id") or (bundle.get("task") or {}).get("task_id") or "")
    top_chart = bundle.get("top_chart") or {}
    quartet = top_chart.get("quartet") or {}

    slot_by_offer_id: dict[str, str] = {}
    for slot_name, slot_offer in quartet.items():
        slot_dict = _as_dict(slot_offer)
        oid = slot_dict.get("offer_id")
        if oid:
            slot_by_offer_id[str(oid)] = str(slot_name)

    ranked_offers = [_as_dict(o) for o in (bundle.get("ranked_offers") or [])]
    if not ranked_offers:
        ranked_offers = [
            _as_dict(o)
            for o in (bundle.get("offers") or [])
            if _as_dict(o).get("deal_badge") not in ("QUARANTINE_LOW_TRUST", "PRUNED_OUTLIER")
        ]

    top_chart_rows: list[list[Any]] = []
    for idx, off in enumerate(ranked_offers, start=1):
        cond = off.get("condition") or {}
        lc = off.get("landed_cost") or {}
        mpns = off.get("mpn_codes") or []
        row = [
            task_id or off.get("task_id", ""),
            off.get("offer_id", ""),
            idx,
            slot_by_offer_id.get(str(off.get("offer_id", "")), ""),
            off.get("deal_badge", ""),
            off.get("channel", ""),
            off.get("route_country", ""),
            off.get("store_name", ""),
            off.get("trust_tier", ""),
            off.get("title", ""),
            ", ".join(str(m) for m in mpns),
            cond.get("code", ""),
            cond.get("grade", ""),
            off.get("price_raw", 0.0),
            off.get("currency", "UAH"),
            lc.get("realistic_landed_uah", 0.0),
            off.get("discount_vs_median_pct", 0.0),
            off.get("effective_quality_score", 0.0),
            off.get("channel_score", 0.0),
            off.get("topsis_score", 0.0),
            off.get("tradeoff_summary_uk", ""),
            off.get("url", ""),
        ]
        # Strictly 22 columns A..V
        assert len(row) == 22
        top_chart_rows.append(row)

    manual_links_rows: list[list[Any]] = []
    for raw_link in bundle.get("manual_links") or []:
        link = _as_dict(raw_link)
        manual_links_rows.append(
            [
                task_id or link.get("task_id", ""),
                link.get("channel", ""),
                link.get("route_country", ""),
                link.get("reason_status", ""),
                link.get("breakeven_max_foreign_price", ""),
                link.get("currency", "UAH"),
                link.get("breakeven_hint_uk", ""),
                link.get("direct_search_url", ""),
            ]
        )

    audit = _as_dict(bundle.get("audit") or {})
    audit_rows: list[list[Any]] = []
    if audit:
        audit_rows.append(
            [
                task_id or audit.get("task_id", ""),
                "SUMMARY",
                "",
                "",
                "ALL",
                "ALL",
                f"Медіана: {round(float(audit.get('reference_median_uah', 0)))} ₴ | IQR: {round(float(audit.get('iqr_uah', 0)))} ₴",
                audit.get("reference_median_uah", 0.0),
                audit.get("pruned_summary_uk", ""),
                "",
            ]
        )

    quarantine_list = bundle.get("quarantine_offers") or audit.get("quarantine_offers") or []
    for raw_q in quarantine_list:
        q = _as_dict(raw_q)
        lc = q.get("landed_cost") or {}
        audit_rows.append(
            [
                task_id or q.get("task_id", ""),
                "QUARANTINE",
                q.get("offer_id", ""),
                q.get("deal_badge", "QUARANTINE_LOW_TRUST"),
                q.get("channel", ""),
                q.get("route_country", ""),
                q.get("title", ""),
                lc.get("realistic_landed_uah", 0.0),
                q.get("prune_reason", ""),
                q.get("url", ""),
            ]
        )

    pruned_list = bundle.get("pruned_offers") or audit.get("pruned_offers") or []
    for raw_p in pruned_list:
        p = _as_dict(raw_p)
        lc = p.get("landed_cost") or {}
        audit_rows.append(
            [
                task_id or p.get("task_id", ""),
                "PRUNED",
                p.get("offer_id", ""),
                p.get("deal_badge", "PRUNED_OUTLIER"),
                p.get("channel", ""),
                p.get("route_country", ""),
                p.get("title", ""),
                lc.get("realistic_landed_uah", 0.0),
                p.get("prune_reason", ""),
                p.get("url", ""),
            ]
        )

    return {
        "task_id": task_id,
        "2_Top_Charts_And_Jackpots": {
            "headers_a_to_v": TOP_CHARTS_HEADERS_A_TO_V,
            "preserved_human_headers_w_to_y": ["Human_Decision", "Target_Price_UAH", "Human_Notes"],
            "rows": top_chart_rows,
        },
        "3_Manual_Check_Links": {
            "headers": MANUAL_LINKS_HEADERS,
            "rows": manual_links_rows,
        },
        "4_Funnel_Audit_And_Quarantine": {
            "headers": FUNNEL_AUDIT_HEADERS,
            "rows": audit_rows,
        },
    }


class GoogleSheetsBridge:
    """
    Async HTTP bridge to Google Apps Script Web App (`Code.gs`).
    - Pulls pending tasks from `1_Task_Queue`.
    - Pushes idempotent 2D arrays for tabs 2, 3, and 4 while preserving human cols W..Y.
    """

    def __init__(
        self,
        webapp_url: str = "",
        sync_secret: str = "",
        timeout: float = 20.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.webapp_url = (webapp_url or os.environ.get("OMNI_GSHEETS_WEBAPP_URL", "")).strip()
        self.sync_secret = (sync_secret or os.environ.get("OMNI_GSHEETS_SYNC_SECRET", "")).strip()
        self.timeout = timeout
        self.transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.webapp_url)

    async def pull_pending_tasks(self) -> list[ShoppingTask]:
        if not self.enabled:
            return []
        params = {"action": "pull_pending_tasks"}
        if self.sync_secret:
            params["secret"] = self.sync_secret

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            resp = await client.get(self.webapp_url, params=params)
            resp.raise_for_status()
            data = resp.json()

        raw_tasks = data.get("tasks") or []
        tasks: list[ShoppingTask] = []
        for item in raw_tasks:
            if isinstance(item, dict):
                tasks.append(ShoppingTask.model_validate(item))
        return tasks

    async def push_harvest_results(self, bundle_or_payload: dict[str, Any]) -> dict[str, Any]:
        payload = (
            bundle_or_payload
            if "2_Top_Charts_And_Jackpots" in bundle_or_payload
            else export_master_sheets_payload(bundle_or_payload)
        )
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "WEBAPP_URL_NOT_CONFIGURED", "payload": payload}

        body = {
            "action": "push_harvest_results",
            "secret": self.sync_secret,
            "payload": payload,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            resp = await client.post(self.webapp_url, json=body)
            resp.raise_for_status()
            return resp.json()
