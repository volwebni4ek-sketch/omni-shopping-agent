from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from omni_agent.core.models import CustomsMode
from omni_agent.storage.sqlite_store import SQLiteStore
from omni_agent.ui.report_renderer import render_standalone_hitl_html


class SearchRequest(BaseModel):
    query: str
    category: str = "general"
    max_budget_uah: Optional[float] = None
    target_price_uah: Optional[float] = None
    estimated_weight_kg: float = 1.5
    ua_only: bool = False
    include_used: bool = False
    demo: bool = True
    customs_mode: Optional[CustomsMode] = None


class DecisionRequest(BaseModel):
    offer_id: str
    decision: str
    target_price_uah: Optional[float] = None
    notes: str = ""


def create_app(
    db_path: str | Path = "data/shopping_hub.db",
    reports_dir: str | Path = "reports",
) -> FastAPI:
    api = FastAPI(
        title="Omni Shopping Agent — HITL Portal",
        version="0.1.0",
        description="Multi-channel UA/EU/Global shopping intelligence & human-in-the-loop decision dashboard.",
    )
    api.state.db_path = str(db_path)
    api.state.reports_dir = str(reports_dir)

    def _get_store() -> SQLiteStore:
        return SQLiteStore(api.state.db_path)

    @api.get("/", response_class=HTMLResponse)
    async def index(task_id: Optional[str] = None) -> HTMLResponse:
        store = _get_store()
        try:
            tasks = store.list_tasks()
            selected_id = task_id or (tasks[0].task_id if tasks else None)
            if selected_id:
                bundle = store.get_task_bundle(selected_id)
                return HTMLResponse(content=render_standalone_hitl_html(bundle))
        finally:
            store.close()

        # Render empty onboarding portal if DB has no tasks yet
        empty_bundle: dict[str, Any] = {
            "task_id": "welcome",
            "task": {"task_id": "welcome", "raw_query": "Запустіть пошук або omni-agent search --demo", "category": "general"},
            "ranked_offers": [],
            "human_review_deals": [],
            "manual_links": [],
            "audit": {},
            "top_chart": {"quartet": {}, "hard_tradeoff_dilemmas": []},
        }
        return HTMLResponse(content=render_standalone_hitl_html(empty_bundle))

    @api.get("/api/tasks")
    async def list_tasks(status: Optional[str] = None) -> list[dict[str, Any]]:
        store = _get_store()
        try:
            tasks = store.list_tasks(status=status)
            return [t.model_dump(mode="json") for t in tasks]
        finally:
            store.close()

    @api.get("/api/tasks/{task_id}")
    async def get_task_bundle(task_id: str) -> dict[str, Any]:
        store = _get_store()
        try:
            task = store.get_task(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
            return store.get_task_bundle(task_id)
        finally:
            store.close()

    @api.post("/api/search")
    async def run_search(req: SearchRequest) -> dict[str, Any]:
        from omni_agent.orchestrator import OmniOrchestrator

        orch = OmniOrchestrator(
            db_path=api.state.db_path,
            reports_dir=api.state.reports_dir,
            demo_mode=req.demo,
        )
        task = orch.build_task_from_query(
            req.query,
            category=req.category,
            max_budget_uah=req.max_budget_uah,
            target_price_uah=req.target_price_uah,
            estimated_weight_kg=req.estimated_weight_kg,
            include_used=req.include_used,
            customs_mode=req.customs_mode,
        )
        return await orch.run_task(
            task,
            ua_only=req.ua_only,
            include_used=req.include_used,
            demo_mode=req.demo,
        )

    @api.post("/api/decisions")
    async def update_decision(req: DecisionRequest) -> dict[str, Any]:
        store = _get_store()
        try:
            updated = store.update_human_decision(
                offer_id=req.offer_id,
                decision=req.decision,
                target_price_uah=req.target_price_uah,
                notes=req.notes,
            )
            return {"ok": True, "decision": updated}
        finally:
            store.close()

    return api


app = create_app()
