from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from omni_agent.connectors.base import ManualCheckLink
from omni_agent.core.funnel import FunnelAuditReport
from omni_agent.core.models import NormalizedOffer, ShoppingTask


def _model_to_dict(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _model_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_model_to_dict(v) for v in obj]
    return obj


class SQLiteStore:
    """
    Persistent SQLite hub for tasks, current offers, price history snapshots,
    statistical funnel audits, manual check links, and human HITL decisions.
    """

    def __init__(self, db_path: str | Path = "data/shopping_hub.db") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    raw_query TEXT NOT NULL,
                    canonical_mpns_json TEXT NOT NULL DEFAULT '[]',
                    queries_i18n_json TEXT NOT NULL DEFAULT '{}',
                    category TEXT NOT NULL DEFAULT 'general',
                    allowed_conditions_json TEXT NOT NULL DEFAULT '[]',
                    max_budget_uah REAL,
                    target_price_uah REAL,
                    urgency_days INTEGER NOT NULL DEFAULT 14,
                    estimated_weight_kg REAL NOT NULL DEFAULT 1.5,
                    volumetric_factor REAL NOT NULL DEFAULT 1.0,
                    customs_mode TEXT NOT NULL DEFAULT 'standard_150eur',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    human_notes TEXT NOT NULL DEFAULT '',
                    task_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS offers_current (
                    offer_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    route_country TEXT NOT NULL,
                    store_name TEXT NOT NULL DEFAULT '',
                    trust_tier INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    price_raw REAL NOT NULL,
                    currency TEXT NOT NULL,
                    realistic_landed_uah REAL NOT NULL,
                    base_landed_uah REAL NOT NULL,
                    effective_quality_score REAL NOT NULL,
                    quality_score REAL NOT NULL,
                    channel_score REAL NOT NULL,
                    topsis_score REAL NOT NULL,
                    is_pareto INTEGER NOT NULL DEFAULT 0,
                    deal_badge TEXT NOT NULL,
                    discount_vs_median_pct REAL NOT NULL DEFAULT 0.0,
                    condition_code TEXT NOT NULL,
                    warranty_months INTEGER NOT NULL DEFAULT 0,
                    delivery_days INTEGER NOT NULL DEFAULT 0,
                    verification_status TEXT NOT NULL,
                    offer_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_offers_current_task
                    ON offers_current(task_id);

                CREATE TABLE IF NOT EXISTS price_history_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    realistic_landed_uah REAL NOT NULL,
                    price_raw REAL NOT NULL,
                    currency TEXT NOT NULL,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_price_history_offer
                    ON price_history_snapshots(offer_id, recorded_at);

                CREATE TABLE IF NOT EXISTS funnel_audits (
                    task_id TEXT PRIMARY KEY,
                    total_raw_offers INTEGER NOT NULL,
                    after_semantic_sieve INTEGER NOT NULL,
                    reference_median_uah REAL NOT NULL,
                    iqr_q1_uah REAL NOT NULL,
                    iqr_q3_uah REAL NOT NULL,
                    iqr_uah REAL NOT NULL,
                    pruned_summary_uk TEXT NOT NULL DEFAULT '',
                    audit_json TEXT NOT NULL,
                    top_chart_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS manual_check_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    route_country TEXT NOT NULL,
                    reason_status TEXT NOT NULL DEFAULT '',
                    breakeven_max_foreign_price REAL,
                    currency TEXT NOT NULL DEFAULT 'UAH',
                    breakeven_hint_uk TEXT NOT NULL DEFAULT '',
                    direct_search_url TEXT NOT NULL,
                    link_json TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_manual_links_task
                    ON manual_check_links(task_id);

                CREATE TABLE IF NOT EXISTS human_decisions (
                    offer_id TEXT PRIMARY KEY,
                    decision TEXT NOT NULL DEFAULT 'PENDING',
                    target_price_uah REAL,
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def upsert_task(self, task: ShoppingTask) -> None:
        task_dict = task.model_dump(mode="json")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, raw_query, canonical_mpns_json, queries_i18n_json,
                    category, allowed_conditions_json, max_budget_uah, target_price_uah,
                    urgency_days, estimated_weight_kg, volumetric_factor, customs_mode,
                    status, human_notes, task_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(task_id) DO UPDATE SET
                    raw_query = excluded.raw_query,
                    canonical_mpns_json = excluded.canonical_mpns_json,
                    queries_i18n_json = excluded.queries_i18n_json,
                    category = excluded.category,
                    allowed_conditions_json = excluded.allowed_conditions_json,
                    max_budget_uah = excluded.max_budget_uah,
                    target_price_uah = excluded.target_price_uah,
                    urgency_days = excluded.urgency_days,
                    estimated_weight_kg = excluded.estimated_weight_kg,
                    volumetric_factor = excluded.volumetric_factor,
                    customs_mode = excluded.customs_mode,
                    status = excluded.status,
                    human_notes = excluded.human_notes,
                    task_json = excluded.task_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    task.task_id,
                    task.raw_query,
                    json.dumps(task.canonical_mpns, ensure_ascii=False),
                    json.dumps(task.queries_i18n, ensure_ascii=False),
                    task.category,
                    json.dumps([c.value for c in task.allowed_conditions], ensure_ascii=False),
                    task.max_budget_uah,
                    task.target_price_uah,
                    task.urgency_days,
                    task.estimated_weight_kg,
                    task.volumetric_factor,
                    task.customs_mode.value,
                    task.status,
                    task.human_notes,
                    json.dumps(task_dict, ensure_ascii=False),
                ),
            )

    def list_tasks(self, status: Optional[str] = None) -> list[ShoppingTask]:
        if status is not None:
            rows = self._conn.execute(
                "SELECT task_json FROM tasks WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT task_json FROM tasks ORDER BY updated_at DESC"
            ).fetchall()
        return [ShoppingTask.model_validate_json(row["task_json"]) for row in rows]

    def get_task(self, task_id: str) -> Optional[ShoppingTask]:
        row = self._conn.execute(
            "SELECT task_json FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        return ShoppingTask.model_validate_json(row["task_json"])

    def save_task_results(
        self,
        task_id: str,
        ranked_offers: list[NormalizedOffer],
        manual_links: list[ManualCheckLink],
        audit: FunnelAuditReport,
        top_chart: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Saves current ranked offers, history snapshots, funnel audit, top-chart quartet,
        and manual check links. Preserves `human_decisions` across re-harvests.
        Returns price-drop / target-price alert dicts.
        """
        prev_rows = self._conn.execute(
            "SELECT offer_id, realistic_landed_uah FROM offers_current WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        prev_prices: dict[str, float] = {
            r["offer_id"]: float(r["realistic_landed_uah"]) for r in prev_rows
        }

        # Also check most recent snapshot for offers that might have rotated out previously
        snap_rows = self._conn.execute(
            """
            SELECT offer_id, realistic_landed_uah
            FROM price_history_snapshots
            WHERE task_id = ?
            ORDER BY id DESC
            """,
            (task_id,),
        ).fetchall()
        for sr in snap_rows:
            if sr["offer_id"] not in prev_prices:
                prev_prices[sr["offer_id"]] = float(sr["realistic_landed_uah"])

        task_row = self._conn.execute(
            "SELECT target_price_uah FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        target_price_uah: Optional[float] = (
            float(task_row["target_price_uah"])
            if task_row is not None and task_row["target_price_uah"] is not None
            else None
        )

        # Deduplicate ranked_offers + human_review_deals preserving ranked order
        offers_to_save: list[NormalizedOffer] = []
        seen_ids: set[str] = set()
        for off in list(ranked_offers) + list(audit.human_review_deals):
            if off.offer_id not in seen_ids:
                seen_ids.add(off.offer_id)
                offers_to_save.append(off)

        alerts: list[dict[str, Any]] = []
        for off in offers_to_save:
            cur_price = float(off.landed_cost.realistic_landed_uah)
            prev_price = prev_prices.get(off.offer_id)
            dropped_vs_prev = prev_price is not None and cur_price < prev_price - 0.01
            hit_target = target_price_uah is not None and target_price_uah > 0 and cur_price <= target_price_uah

            if dropped_vs_prev or hit_target:
                drop_uah = round((prev_price - cur_price), 2) if prev_price is not None else 0.0
                drop_pct = round((drop_uah / prev_price) * 100.0, 1) if prev_price and prev_price > 0 else 0.0
                if dropped_vs_prev and hit_target:
                    alert_type = "PRICE_DROP_AND_TARGET_HIT"
                    msg_uk = (
                        f"Зниження ціни -{round(drop_uah)} ₴ (-{drop_pct:.1f}%) і досягнуто ціль "
                        f"≤ {round(target_price_uah or 0)} ₴: {off.title} ({round(cur_price)} ₴)"
                    )
                elif dropped_vs_prev:
                    alert_type = "PRICE_DROP_VS_PREVIOUS"
                    msg_uk = (
                        f"Ціна знизилась з {round(prev_price or 0)} ₴ до {round(cur_price)} ₴ "
                        f"(-{round(drop_uah)} ₴ / -{drop_pct:.1f}%): {off.title}"
                    )
                else:
                    alert_type = "TARGET_PRICE_REACHED"
                    msg_uk = (
                        f"Досягнуто цільову ціну ≤ {round(target_price_uah or 0)} ₴: "
                        f"{off.title} за {round(cur_price)} ₴ ({off.channel})"
                    )
                alerts.append(
                    {
                        "alert_type": alert_type,
                        "task_id": task_id,
                        "offer_id": off.offer_id,
                        "title": off.title,
                        "channel": off.channel,
                        "previous_price_uah": round(prev_price, 2) if prev_price is not None else None,
                        "current_price_uah": round(cur_price, 2),
                        "target_price_uah": target_price_uah,
                        "drop_uah": drop_uah,
                        "drop_pct": drop_pct,
                        "url": off.url,
                        "message_uk": msg_uk,
                    }
                )

        serialized_top_chart = _model_to_dict(top_chart)
        serialized_audit = audit.model_dump(mode="json")

        with self._conn:
            self._conn.execute("DELETE FROM offers_current WHERE task_id = ?", (task_id,))
            for off in offers_to_save:
                off_json = off.model_dump_json()
                self._conn.execute(
                    """
                    INSERT INTO offers_current (
                        offer_id, task_id, channel, route_country, store_name, trust_tier,
                        title, url, price_raw, currency, realistic_landed_uah, base_landed_uah,
                        effective_quality_score, quality_score, channel_score, topsis_score,
                        is_pareto, deal_badge, discount_vs_median_pct, condition_code,
                        warranty_months, delivery_days, verification_status, offer_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        off.offer_id,
                        task_id,
                        off.channel,
                        off.route_country,
                        off.store_name,
                        int(off.trust_tier.value),
                        off.title,
                        off.url,
                        float(off.price_raw),
                        off.currency,
                        float(off.landed_cost.realistic_landed_uah),
                        float(off.landed_cost.base_landed_uah),
                        float(off.effective_quality_score),
                        float(off.quality_score),
                        float(off.channel_score),
                        float(off.topsis_score),
                        1 if off.is_pareto else 0,
                        off.deal_badge.value,
                        float(off.discount_vs_median_pct),
                        off.condition.code.value,
                        int(off.warranty_months),
                        int(off.delivery_days),
                        off.verification_status.value,
                        off_json,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO price_history_snapshots (
                        offer_id, task_id, channel, realistic_landed_uah, price_raw, currency
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        off.offer_id,
                        task_id,
                        off.channel,
                        float(off.landed_cost.realistic_landed_uah),
                        float(off.price_raw),
                        off.currency,
                    ),
                )

            self._conn.execute(
                """
                INSERT INTO funnel_audits (
                    task_id, total_raw_offers, after_semantic_sieve, reference_median_uah,
                    iqr_q1_uah, iqr_q3_uah, iqr_uah, pruned_summary_uk, audit_json, top_chart_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(task_id) DO UPDATE SET
                    total_raw_offers = excluded.total_raw_offers,
                    after_semantic_sieve = excluded.after_semantic_sieve,
                    reference_median_uah = excluded.reference_median_uah,
                    iqr_q1_uah = excluded.iqr_q1_uah,
                    iqr_q3_uah = excluded.iqr_q3_uah,
                    iqr_uah = excluded.iqr_uah,
                    pruned_summary_uk = excluded.pruned_summary_uk,
                    audit_json = excluded.audit_json,
                    top_chart_json = excluded.top_chart_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    task_id,
                    audit.total_raw_offers,
                    audit.after_semantic_sieve,
                    audit.reference_median_uah,
                    audit.iqr_q1_uah,
                    audit.iqr_q3_uah,
                    audit.iqr_uah,
                    audit.pruned_summary_uk,
                    json.dumps(serialized_audit, ensure_ascii=False),
                    json.dumps(serialized_top_chart, ensure_ascii=False),
                ),
            )

            self._conn.execute("DELETE FROM manual_check_links WHERE task_id = ?", (task_id,))
            for link in manual_links:
                self._conn.execute(
                    """
                    INSERT INTO manual_check_links (
                        task_id, channel, route_country, reason_status,
                        breakeven_max_foreign_price, currency, breakeven_hint_uk,
                        direct_search_url, link_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        task_id,
                        link.channel,
                        link.route_country,
                        link.reason_status,
                        link.breakeven_max_foreign_price,
                        link.currency,
                        link.breakeven_hint_uk,
                        link.direct_search_url,
                        link.model_dump_json(),
                    ),
                )

        return alerts

    def update_human_decision(
        self,
        offer_id: str,
        decision: str,
        target_price_uah: Optional[float] = None,
        notes: str = "",
    ) -> dict[str, Any]:
        clean_decision = (decision or "PENDING").strip().upper()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO human_decisions (offer_id, decision, target_price_uah, notes, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(offer_id) DO UPDATE SET
                    decision = excluded.decision,
                    target_price_uah = excluded.target_price_uah,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (offer_id, clean_decision, target_price_uah, notes or ""),
            )
        row = self._conn.execute(
            "SELECT offer_id, decision, target_price_uah, notes, updated_at FROM human_decisions WHERE offer_id = ?",
            (offer_id,),
        ).fetchone()
        return dict(row) if row else {
            "offer_id": offer_id,
            "decision": clean_decision,
            "target_price_uah": target_price_uah,
            "notes": notes or "",
        }

    def get_human_decisions(self, offer_ids: Optional[list[str]] = None) -> dict[str, dict[str, Any]]:
        if offer_ids is not None:
            if not offer_ids:
                return {}
            placeholders = ",".join("?" for _ in offer_ids)
            rows = self._conn.execute(
                f"SELECT offer_id, decision, target_price_uah, notes, updated_at FROM human_decisions WHERE offer_id IN ({placeholders})",
                offer_ids,
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT offer_id, decision, target_price_uah, notes, updated_at FROM human_decisions"
            ).fetchall()
        return {r["offer_id"]: dict(r) for r in rows}

    def get_task_bundle(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        task_dict = task.model_dump(mode="json") if task else None

        offer_rows = self._conn.execute(
            """
            SELECT offer_json FROM offers_current
            WHERE task_id = ?
            ORDER BY topsis_score DESC, realistic_landed_uah ASC
            """,
            (task_id,),
        ).fetchall()
        offers = [json.loads(r["offer_json"]) for r in offer_rows]

        decisions = self.get_human_decisions([o["offer_id"] for o in offers])
        for o in offers:
            dec = decisions.get(o["offer_id"])
            o["human_decision"] = dec["decision"] if dec else "PENDING"
            o["human_target_price_uah"] = dec["target_price_uah"] if dec else None
            o["human_notes"] = dec["notes"] if dec else ""

        audit_row = self._conn.execute(
            "SELECT audit_json, top_chart_json, updated_at FROM funnel_audits WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        audit_dict = json.loads(audit_row["audit_json"]) if audit_row else {}
        top_chart_dict = json.loads(audit_row["top_chart_json"]) if audit_row else {}

        # Merge human decisions into quartet slots as well
        quartet = top_chart_dict.get("quartet") or {}
        for slot_key, slot_offer in list(quartet.items()):
            if isinstance(slot_offer, dict) and slot_offer.get("offer_id"):
                dec = decisions.get(slot_offer["offer_id"])
                slot_offer["human_decision"] = dec["decision"] if dec else "PENDING"
                slot_offer["human_target_price_uah"] = dec["target_price_uah"] if dec else None
                slot_offer["human_notes"] = dec["notes"] if dec else ""

        link_rows = self._conn.execute(
            "SELECT link_json FROM manual_check_links WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        manual_links = [json.loads(r["link_json"]) for r in link_rows]

        hist_rows = self._conn.execute(
            """
            SELECT offer_id, channel, realistic_landed_uah, price_raw, currency, recorded_at
            FROM price_history_snapshots
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT 200
            """,
            (task_id,),
        ).fetchall()
        price_history = [dict(r) for r in hist_rows]

        ranked_offers = [
            o for o in offers
            if o.get("deal_badge") not in ("QUARANTINE_LOW_TRUST", "PRUNED_OUTLIER")
        ]
        human_review_deals = [
            o for o in offers
            if o.get("deal_badge") == "HUMAN_REVIEW_DEAL"
        ]

        return {
            "task_id": task_id,
            "task": task_dict,
            "offers": offers,
            "ranked_offers": ranked_offers,
            "human_review_deals": human_review_deals,
            "quarantine_offers": audit_dict.get("quarantine_offers", []),
            "pruned_offers": audit_dict.get("pruned_offers", []),
            "manual_links": manual_links,
            "audit": audit_dict,
            "top_chart": top_chart_dict,
            "human_decisions": decisions,
            "price_history": price_history,
            "updated_at": audit_row["updated_at"] if audit_row else None,
        }
