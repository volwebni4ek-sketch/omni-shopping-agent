from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

import httpx


class OptionalGeminiReviewer:
    """
    Lightweight optional Google AI Studio client (model: gemini-2.5-flash via httpx).
    - Strictly disabled when GEMINI_API_KEY is empty -> returns deterministic local summary at 0 cost.
    - SQLite table cache (`llm_eval_cache` keyed by SHA-256 hash).
    - Hard cap `max_calls_per_task = 5`.
    """

    GEMINI_ENDPOINT = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent"
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        db_path: Optional[str | Path] = None,
        max_calls_per_task: int = 5,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")).strip()
        self.db_path = str(db_path or Path("data") / "llm_eval_cache.sqlite")
        self.max_calls_per_task = max_calls_per_task
        self.timeout_seconds = timeout_seconds
        self._task_call_counts: dict[str, int] = {}
        self._init_db()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _init_db(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_eval_cache (
                    cache_key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    @staticmethod
    def _compute_cache_key(task_id: str, prompt_payload: dict[str, Any]) -> str:
        raw = json.dumps({"task_id": task_id, "payload": prompt_payload}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _get_cached(self, cache_key: str) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT response_json FROM llm_eval_cache WHERE cache_key = ?",
                (cache_key,),
            )
            row = cur.fetchone()
            if row:
                return json.loads(row[0])
        return None

    def _set_cached(self, cache_key: str, task_id: str, result: dict[str, Any]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO llm_eval_cache (cache_key, model, task_id, response_json)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, "gemini-2.5-flash", task_id, json.dumps(result, ensure_ascii=False)),
            )
            conn.commit()

    @staticmethod
    def _deterministic_local_summary(prompt_payload: dict[str, Any], reason: str) -> dict[str, Any]:
        candidates = prompt_payload.get("candidates", [])
        top_title = candidates[0].get("title", "Немає кандидатів") if candidates else "Немає кандидатів"
        top_price = candidates[0].get("realistic_landed_uah", 0) if candidates else 0
        return {
            "source": "deterministic_local",
            "reason": reason,
            "summary_uk": (
                f"Локальний інженерний підсумок (0 LLM-викликів): лідер рейтингу TOPSIS — "
                f"{top_title} ({round(float(top_price))} ₴ з доставкою та митом)."
            ),
            "warnings_uk": [],
        }

    async def review_shortlist(
        self,
        task_id: str,
        prompt_payload: dict[str, Any],
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> dict[str, Any]:
        """
        Optionally asks gemini-2.5-flash for a concise Ukrainian audit of the shortlist.
        Checks SQLite cache first; enforces max_calls_per_task and zero-cost fallback when disabled.
        """
        cache_key = self._compute_cache_key(task_id, prompt_payload)
        cached = self._get_cached(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached

        if not self.enabled:
            return self._deterministic_local_summary(prompt_payload, reason="GEMINI_API_KEY_NOT_SET")

        used_calls = self._task_call_counts.get(task_id, 0)
        if used_calls >= self.max_calls_per_task:
            return self._deterministic_local_summary(
                prompt_payload,
                reason=f"MAX_CALLS_REACHED ({self.max_calls_per_task})",
            )

        self._task_call_counts[task_id] = used_calls + 1

        prompt_text = (
            "Ти інженерний асистент із закупівель. Надай стислий аудит українською (2-3 речення) "
            "для наступного шортлиста товарів з урахуванням ціни з доставкою/митом та ризиків:\n"
            + json.dumps(prompt_payload, ensure_ascii=False)
        )

        request_body = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400},
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=transport) as client:
                resp = await client.post(
                    self.GEMINI_ENDPOINT,
                    params={"key": self.api_key},
                    json=request_body,
                )
                resp.raise_for_status()
                data = resp.json()

            text_out = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
                .strip()
            )
            if not text_out:
                return self._deterministic_local_summary(prompt_payload, reason="EMPTY_GEMINI_RESPONSE")

            result = {
                "source": "gemini-2.5-flash",
                "cache_hit": False,
                "summary_uk": text_out,
                "warnings_uk": [],
            }
            self._set_cached(cache_key, task_id, result)
            return result
        except Exception as exc:
            return self._deterministic_local_summary(
                prompt_payload,
                reason=f"GEMINI_FALLBACK: {type(exc).__name__}",
            )
