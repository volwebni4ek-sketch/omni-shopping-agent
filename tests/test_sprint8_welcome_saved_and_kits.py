from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from omni_agent.core.models import ShoppingTask, VerificationStatus
from omni_agent.core.normalizer import is_accessory_or_part
from omni_agent.storage.relay_ingest import (
    ingest_relay_file,
    relay_offer_to_normalized_offer,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RELAY_SCRIPT_PATH = REPO_ROOT / "local_relay" / "omni_residential_harvester.py"


def _load_relay_module():
    spec = importlib.util.spec_from_file_location("omni_residential_harvester", RELAY_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["omni_residential_harvester"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_aliexpress_welcome_deal_unmasking_and_markdown_tag() -> None:
    relay = _load_relay_module()
    task = {
        "task_id": "T-ALI-WELCOME",
        "raw_query": "ALIENTEK T90B soldering iron",
        "category": "general",
    }
    html = """
    <html><body>
      <a href="https://www.aliexpress.com/item/100500888777.html">
        ALIENTEK T90B 140W Smart Soldering Iron US $33.09 New shoppers - US $8.13 1,200+ sold Free shipping
      </a>
      <a href="https://www.aliexpress.com/item/100500888999.html">
        ALIENTEK T80P Soldering Iron US $29.50 New shoppers save $5.50 Free shipping
      </a>
    </body></html>
    """
    offers, ml = relay.parse_aliexpress_html(
        html,
        task,
        "https://www.aliexpress.com/wholesale?SearchText=ALIENTEK+T90B",
    )
    assert ml is None
    assert len(offers) == 2

    first = offers[0]
    assert first["price_raw"] == 41.22
    assert first["raw_price_snippet"] == "US $41.22 (welcome $33.09 + $8.13)"
    assert first["specs"]["is_welcome_deal_adjusted"] is True
    assert first["specs"]["welcome_price_usd"] == 33.09
    assert first["specs"]["welcome_discount_usd"] == 8.13

    second = offers[1]
    assert second["price_raw"] == 35.0
    assert second["specs"]["is_welcome_deal_adjusted"] is True
    assert second["specs"]["welcome_price_usd"] == 29.5
    assert second["specs"]["welcome_discount_usd"] == 5.5

    md = relay.build_copy_to_antigravity_markdown(
        {
            "schema_version": "1.0",
            "generated_at": "2026-11-25T18:00:00Z",
            "source_ip_mode": "residential_local",
            "tasks": [{**task, "offers": offers, "manual_links": [], "channel_summary": {}}],
        }
    )
    assert "(реальна ціна без Welcome-бонусу; для нового акаунта $33.09)" in md
    assert "(реальна ціна без Welcome-бонусу; для нового акаунта $29.50)" in md


def test_parse_saved_pages_dir_olx_and_aliexpress_and_cli_mode(tmp_path: Path) -> None:
    relay = _load_relay_module()
    saved_dir = tmp_path / "saved_pages"
    saved_dir.mkdir(parents=True, exist_ok=True)

    olx_file = saved_dir / "olx_soldering.html"
    olx_file.write_text(
        """
        <html><head><title>OLX.ua</title></head><body>
          <div data-cy="l-card">
            <a href="/d/uk/obyavlenie/alientek-t90b-kyiv-IDOlxSaved1.html"><h4>Паяльник Alientek T90B 140W</h4></a>
            <p>2 150 грн</p>
            <span>Нове</span>
            <span>Київ, Солом'янський - Сьогодні</span>
          </div>
        </body></html>
        """,
        encoding="utf-8",
    )

    ali_file = saved_dir / "aliexpress_t90b.htm"
    ali_file.write_text(
        """
        <html><body>
          <a href="https://www.aliexpress.com/item/100500123456.html">
            ALIENTEK T90B Soldering Iron 140W PD3.1 US $33.09 New shoppers - US $8.13 Free shipping
          </a>
        </body></html>
        """,
        encoding="utf-8",
    )

    assert relay.detect_saved_page_channel(olx_file.read_text(encoding="utf-8"), olx_file.name) == "olx"
    assert relay.detect_saved_page_channel(ali_file.read_text(encoding="utf-8"), ali_file.name) == "aliexpress"

    task_bundle = relay.parse_saved_pages_dir(saved_dir)
    assert task_bundle["task_id"] == "SAVED_BROWSER_IMPORT"
    assert "Ctrl+S" in task_bundle["raw_query"]
    assert len(task_bundle["offers"]) == 2
    by_ch = {o["channel"]: o for o in task_bundle["offers"]}
    assert by_ch["olx"]["url"] == "https://www.olx.ua/d/uk/obyavlenie/alientek-t90b-kyiv-IDOlxSaved1.html"
    assert by_ch["olx"]["price_raw"] == 2150.0
    assert by_ch["aliexpress"]["price_raw"] == 41.22
    assert task_bundle["channel_summary"]["olx"]["status"] == "VERIFIED"
    assert task_bundle["channel_summary"]["aliexpress"]["status"] == "VERIFIED"

    # Verify CLI --parse-saved --saved-dir runs offline and emits valid JSON & Markdown
    out_dir = tmp_path / "out"
    rc = relay.main(["--parse-saved", "--saved-dir", str(saved_dir), "--output-dir", str(out_dir)])
    assert rc == 0
    latest_json = json.loads((out_dir / "latest_relay_result.json").read_text(encoding="utf-8"))
    assert latest_json["source_ip_mode"] == "saved_browser_html"
    assert len(latest_json["tasks"]) == 1
    assert len(latest_json["tasks"][0]["offers"]) == 2
    assert (out_dir / "COPY_TO_ANTIGRAVITY.md").is_file()


def test_kit_aware_is_accessory_or_part() -> None:
    query = "Паяльник Alientek T90B"
    # Should prune standalone mats, tips, chargers, holders
    assert is_accessory_or_part("Професійний силіконовий килимок S-160", query) is True
    assert is_accessory_or_part("Силиконовый коврик для пайки 450x300", query) is True
    assert is_accessory_or_part("Жало C245 для паяльника", query) is True
    assert is_accessory_or_part("Блок живлення 140W для паяльника", query) is True
    assert is_accessory_or_part("Тримач для паяльника з губкою", query) is True

    # Should KEEP main device + accessory kits/bundles
    assert is_accessory_or_part("Набір паяльник Alientek T90B + блок живлення 140W", query) is False
    assert is_accessory_or_part("Паяльник Alientek T90B з футляром та підставкою", query) is False
    assert is_accessory_or_part("Комплект паяльник Alientek T90B з жалами C245 та кабелем", query) is False
    assert is_accessory_or_part("Alientek T90B soldering iron set with 140W charger", query) is False


@pytest.mark.asyncio
async def test_relay_ingest_keeps_rozetka_2099_verified_and_grounded(tmp_path: Path) -> None:
    task = ShoppingTask(
        task_id="TASK-RZ-2099",
        raw_query="Паяльник Alientek T90B",
        category="general",
    )

    for snippet in ("2099 ₴", "2 099 ₴", "2\u2009099 ₴", "2099.0 UAH", "2&nbsp;099&nbsp;₴"):
        norm = relay_offer_to_normalized_offer(
            {
                "offer_id": "rz_2099",
                "channel": "rozetka",
                "route_country": "UA",
                "store_name": "Rozetka.ua",
                "trust_tier": 1,
                "title": "Паяльник Alientek T90B 140W Type-C",
                "url": "https://rozetka.com.ua/ua/alientek-t90b/p4442099/",
                "price_raw": 2099.0,
                "currency": "UAH",
                "raw_price_snippet": snippet,
                "raw_condition": "Нове",
            },
            task,
        )
        assert norm.is_grounded is True, f"Failed grounding for snippet {snippet!r}"
        assert norm.verification_status == VerificationStatus.VERIFIED

    bundle = {
        "schema_version": "1.0",
        "generated_at": "2026-11-25T18:10:00Z",
        "source_ip_mode": "residential_local",
        "tasks": [
            {
                "task_id": "TASK-RZ-2099",
                "raw_query": "Паяльник Alientek T90B",
                "category": "general",
                "offers": [
                    {
                        "offer_id": "rz_2099_main",
                        "channel": "rozetka",
                        "route_country": "UA",
                        "store_name": "Rozetka.ua",
                        "trust_tier": 1,
                        "title": "Паяльник Alientek T90B 140W Type-C",
                        "url": "https://rozetka.com.ua/ua/alientek-t90b/p4442099/",
                        "price_raw": 2099.0,
                        "currency": "UAH",
                        "raw_price_snippet": "2 099 ₴",
                        "raw_condition": "Нове",
                    },
                    {
                        "offer_id": "rz_2099_uah",
                        "channel": "rozetka",
                        "route_country": "UA",
                        "store_name": "Rozetka.ua",
                        "trust_tier": 1,
                        "title": "Паяльник Alientek T90B 140W Kit",
                        "url": "https://rozetka.com.ua/ua/alientek-t90b-kit/p4442100/",
                        "price_raw": 2099.0,
                        "currency": "UAH",
                        "raw_price_snippet": "2099.0 UAH",
                        "raw_condition": "Нове",
                    },
                ],
                "manual_links": [],
            }
        ],
    }

    relay_path = tmp_path / "latest_relay_result.json"
    relay_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    results = await ingest_relay_file(
        relay_path,
        db_path=tmp_path / "hub.db",
        reports_dir=tmp_path / "reports",
    )
    assert len(results) == 1
    ranked = results[0]["ranked_offers"]
    assert len(ranked) == 2
    for offer in ranked:
        assert offer["verification_status"] == VerificationStatus.VERIFIED.value
        assert offer["is_grounded"] is True
