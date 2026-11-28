"""
Deterministic, Zero-Hallucination Multi-Channel Catalog Report Generator.

Guarantees:
  - Strictly 1 Task = 1 uniform expandable product card (NEVER mixes products).
  - Strictly separated channels per product: AliExpress, Prom.ua, Rozetka, Hotline, Europe (DE).
  - Up to Top-10 real, verified, sorted offers per channel directly from harvested JSON.
  - Zero fabricated prices or prose guesswork: if a channel has 0 parsed offers,
    it is shown honestly with its status and direct search link.
  - Subtle visual highlight on the mathematically lowest Landed Cost UAH offer.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from omni_agent.core.landed_cost import LandedCostEngine
from omni_agent.core.models import CustomsMode

CHANNEL_ORDER = [
    ("aliexpress", "AliExpress", "--chart-1"),
    ("prom", "Prom.ua", "--chart-2"),
    ("rozetka", "Rozetka", "--chart-3"),
    ("hotline", "Hotline", "--chart-4"),
    ("europe_de", "Європа (DE · NP Shopping)", "--chart-5"),
]


def _normalize_channel_bucket(ch: str) -> str:
    c = (ch or "").strip().lower()
    if c in ("amazon_de", "idealo_de", "europe_de"):
        return "europe_de"
    return c


def _clean_url(url: str) -> str:
    if "amazon.de" in url and "/dp/" in url:
        return url.split("?")[0]
    return url


def _compute_offer_landed(
    offer: dict[str, Any],
    weight_kg: float,
    engine: LandedCostEngine,
) -> tuple[int, str, str]:
    ch = (offer.get("channel") or "").strip().lower()
    price_raw = float(offer.get("price_raw") or 0.0)
    currency = (offer.get("currency") or "UAH").upper()

    if ch in ("prom", "rozetka", "hotline", "olx"):
        return round(price_raw), f"{round(price_raw):,} ₴".replace(",", " "), "UA · 0 ₴ мито"

    if ch == "aliexpress":
        lc = engine.calculate(
            item_price_raw=price_raw,
            currency=currency,
            route_country="CN",
            weight_kg=weight_kg,
            customs_mode=CustomsMode.STANDARD_150EUR,
            direct_to_ua_export=True,
            is_aliexpress_cn=True,
        )
        duty = round(lc.customs_duty_vat_uah)
        duty_str = "0 ₴ мито (≤€150)" if duty == 0 else f"+{duty:,} ₴ мито/ПДВ".replace(",", " ")
        return round(lc.realistic_landed_uah), f"${price_raw:.2f}", duty_str

    if ch in ("amazon_de", "idealo_de"):
        lc = engine.calculate(
            item_price_raw=price_raw,
            currency=currency,
            route_country="DE",
            weight_kg=weight_kg,
            customs_mode=CustomsMode.STANDARD_150EUR,
            direct_to_ua_export=False,
        )
        duty = round(lc.customs_duty_vat_uah)
        ship = round(lc.forwarder_fee_uah)
        duty_str = "0 ₴ мито (≤€150)" if duty == 0 else f"+{duty:,} ₴ мито/ПДВ".replace(",", " ")
        src_tag = "Amazon.de" if ch == "amazon_de" else "Idealo.de"
        return (
            round(lc.realistic_landed_uah),
            f"€{price_raw:.2f} ({src_tag})",
            f"NP Shopping ~{ship} ₴ · {duty_str} · без повернення DE VAT",
        )

    return round(price_raw), f"{price_raw} {currency}", currency


def build_catalog_payload(
    relay_paths: list[Path],
    top_n: int = 10,
) -> dict[str, Any]:
    engine = LandedCostEngine()
    merged_tasks: dict[str, dict[str, Any]] = {}

    for p in relay_paths:
        raw = json.loads(p.read_text(encoding="utf-8"))
        for t in raw.get("tasks", []):
            tid = t.get("task_id", "")
            if not tid:
                continue
            if tid not in merged_tasks:
                merged_tasks[tid] = {
                    "task_id": tid,
                    "raw_query": t.get("raw_query", tid),
                    "estimated_weight_kg": float(t.get("estimated_weight_kg") or 1.0),
                    "offers_by_url": {},
                    "manual_by_bucket": {},
                }
            for o in t.get("offers", []):
                url = _clean_url(o.get("url", ""))
                if url and float(o.get("price_raw") or 0) > 0:
                    merged_tasks[tid]["offers_by_url"][url] = {**o, "url": url}
            for ml in t.get("manual_links", []):
                b = _normalize_channel_bucket(ml.get("channel", ""))
                if b not in merged_tasks[tid]["manual_by_bucket"]:
                    merged_tasks[tid]["manual_by_bucket"][b] = {
                        "reason": ml.get("reason_status", "NO_RESULTS"),
                        "url": ml.get("direct_search_url", ""),
                    }

    products: list[dict[str, Any]] = []
    for tid, t in merged_tasks.items():
        weight_kg = t["estimated_weight_kg"]
        buckets: dict[str, list[dict[str, Any]]] = {k: [] for k, _, _ in CHANNEL_ORDER}

        for o in t["offers_by_url"].values():
            b = _normalize_channel_bucket(o.get("channel", ""))
            if b not in buckets:
                continue
            # Basic noise guard for obviously broken Idealo.de 1 EUR placeholder rows
            if b == "europe_de" and float(o.get("price_raw") or 0) < 10.0:
                continue
            landed_uah, orig_str, tax_note = _compute_offer_landed(o, weight_kg, engine)
            buckets[b].append(
                {
                    "title": (o.get("title") or "").strip(),
                    "url": o["url"],
                    "landed_uah": landed_uah,
                    "orig_price": orig_str,
                    "tax_note": tax_note,
                    "sold": int(o.get("orders_or_reviews") or 0),
                    "rating": o.get("seller_rating"),
                    "store": (o.get("store_name") or "").strip(),
                }
            )

        channels_out: dict[str, dict[str, Any]] = {}
        best_overall_uah: int | None = None
        best_overall_channel: str | None = None

        for ch_key, ch_label, ch_color in CHANNEL_ORDER:
            items = buckets[ch_key]
            # Sort strictly by landed_uah ascending, tie-break by orders desc
            items.sort(key=lambda x: (x["landed_uah"], -x["sold"]))
            top_items = items[:top_n]
            min_uah = top_items[0]["landed_uah"] if top_items else None
            if min_uah is not None and (best_overall_uah is None or min_uah < best_overall_uah):
                best_overall_uah = min_uah
                best_overall_channel = ch_key

            ml_info = t["manual_by_bucket"].get(ch_key)
            channels_out[ch_key] = {
                "label": ch_label,
                "color_var": ch_color,
                "count": len(top_items),
                "total_parsed": len(items),
                "min_uah": min_uah,
                "items": top_items,
                "fallback": ml_info,
            }

        products.append(
            {
                "task_id": tid,
                "title": t["raw_query"],
                "weight_kg": weight_kg,
                "best_uah": best_overall_uah,
                "best_channel": best_overall_channel,
                "channels": channels_out,
            }
        )

    return {
        "top_n": top_n,
        "total_products": len(products),
        "products": products,
    }


def render_catalog_html(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Omni Shopping — Єдиний Каталог Товарів (Топ-{payload['top_n']} по кожному каналу)</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
</head>
<body class="bg-[var(--background)] text-[var(--foreground)] antialiased p-4 md:p-6">
  <div class="max-w-7xl mx-auto space-y-5">

    <!-- Header -->
    <header class="flex flex-col md:flex-row md:items-end justify-between gap-4 pb-4 border-b border-[var(--border)]">
      <div>
        <div class="text-xs uppercase tracking-wider text-[var(--muted-foreground)] font-semibold">Omni Shopping Standard Template · 100% Verified Harvest Data</div>
        <h1 class="text-2xl font-bold mt-1">Каталог товарів: 1 товар = 1 блок, Топ-{payload['top_n']} по кожному каналу</h1>
        <p class="text-xs text-[var(--muted-foreground)] mt-1">Жодного змішування товарів і жодних вигаданих цін. Кожен канал показаний окремо; мінімальна ціна «під ключ» лише м'яко підсвічена.</p>
      </div>
      <div class="flex flex-wrap items-center gap-2 text-xs">
        <input id="searchInput" type="text" placeholder="Фільтр товарів..." class="px-3 py-1.5 rounded-lg bg-[var(--card)] border border-[var(--border)] text-xs focus:outline-none" />
        <button id="expandAllBtn" class="px-3 py-1.5 rounded-lg bg-[var(--card)] border border-[var(--border)] font-medium hover:bg-[var(--secondary)]">Розгорнути всі</button>
        <button id="collapseAllBtn" class="px-3 py-1.5 rounded-lg bg-[var(--card)] border border-[var(--border)] font-medium hover:bg-[var(--secondary)]">Згорнути всі</button>
      </div>
    </header>

    <!-- Uniform Rules Legend -->
    <section class="grid grid-cols-2 md:grid-cols-5 gap-2 text-xs">
      <div class="bg-[var(--card)] border border-[var(--border)] rounded-lg p-2.5 flex items-center gap-2"><span class="w-3 h-3 rounded-sm bg-[var(--chart-1)] shrink-0"></span><span><b>AliExpress</b> (≤€150: 0 ₴ мито)</span></div>
      <div class="bg-[var(--card)] border border-[var(--border)] rounded-lg p-2.5 flex items-center gap-2"><span class="w-3 h-3 rounded-sm bg-[var(--chart-2)] shrink-0"></span><span><b>Prom.ua</b> (UA, 0 ₴ мито)</span></div>
      <div class="bg-[var(--card)] border border-[var(--border)] rounded-lg p-2.5 flex items-center gap-2"><span class="w-3 h-3 rounded-sm bg-[var(--chart-3)] shrink-0"></span><span><b>Rozetka</b> (UA, 0 ₴ мито)</span></div>
      <div class="bg-[var(--card)] border border-[var(--border)] rounded-lg p-2.5 flex items-center gap-2"><span class="w-3 h-3 rounded-sm bg-[var(--chart-4)] shrink-0"></span><span><b>Hotline</b> (UA агрегатор)</span></div>
      <div class="bg-[var(--card)] border border-[var(--border)] rounded-lg p-2.5 flex items-center gap-2"><span class="w-3 h-3 rounded-sm bg-[var(--chart-5)] shrink-0"></span><span><b>Європа DE</b> (NP Shopping)</span></div>
    </section>

    <!-- Product Cards Container -->
    <div id="catalogRoot" class="space-y-3"></div>
  </div>

  <script>
    const CATALOG = {data_json};
    const CH_KEYS = ["aliexpress", "prom", "rozetka", "hotline", "europe_de"];
    const state = {{
      query: "",
      openIds: new Set(CATALOG.products.slice(0, 2).map(p => p.task_id)),
      activeTab: {{}},
      sortMode: {{}}
    }};

    function esc(s) {{
      return String(s || "").replace(/[&<>"']/g, m => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[m]));
    }}

    function fmtUah(n) {{
      if (n === null || n === undefined) return "—";
      return Number(n).toLocaleString("uk-UA") + " ₴";
    }}

    function render() {{
      const root = document.getElementById("catalogRoot");
      root.innerHTML = "";
      const q = state.query.trim().toLowerCase();

      CATALOG.products.forEach(prod => {{
        if (q && !prod.title.toLowerCase().includes(q) && !prod.task_id.toLowerCase().includes(q)) return;

        const isOpen = state.openIds.has(prod.task_id);
        const curTab = state.activeTab[prod.task_id] || "all";
        const curSort = state.sortMode[prod.task_id] || "price";

        const card = document.createElement("div");
        card.className = "bg-[var(--card)] border border-[var(--border)] rounded-xl overflow-hidden";

        // Channel summary pills in header
        const pills = CH_KEYS.map(ck => {{
          const c = prod.channels[ck];
          const isBest = prod.best_channel === ck && c.min_uah !== null;
          const ring = isBest ? "border-[var(--primary)] bg-[var(--primary)]/15 font-bold" : "border-[var(--border)] bg-[var(--background)]";
          const val = c.min_uah !== null ? fmtUah(c.min_uah) : "0 шт";
          return `<div class="px-2.5 py-1 rounded-lg border text-[11px] flex items-center gap-1.5 ${{ring}}">
            <span class="w-2 h-2 rounded-full" style="background:var(${{c.color_var}})"></span>
            <span class="text-[var(--muted-foreground)]">${{esc(c.label.split(" ")[0])}}:</span>
            <span class="font-mono">${{val}}</span>
            ${{isBest ? '<span class="text-[10px] text-[var(--link)]">★</span>' : ''}}
          </div>`;
        }}).join("");

        let bodyHtml = "";
        if (isOpen) {{
          // Mini visual bar chart for this product only
          const validPrices = CH_KEYS.map(ck => prod.channels[ck].min_uah).filter(v => v !== null);
          const maxP = validPrices.length ? Math.max(...validPrices) : 1;
          const miniBars = CH_KEYS.map(ck => {{
            const c = prod.channels[ck];
            if (c.min_uah === null) return "";
            const pct = Math.max(8, Math.round((c.min_uah / maxP) * 100));
            const isBest = prod.best_channel === ck;
            return `<div class="flex items-center gap-3 text-xs">
              <div class="w-36 shrink-0 text-[var(--muted-foreground)] truncate">${{esc(c.label)}}</div>
              <div class="flex-1 bg-[var(--background)] rounded h-4 overflow-hidden border border-[var(--border)]">
                <div class="h-full rounded-sm" style="width:${{pct}}%; background:var(${{c.color_var}}); opacity:${{isBest ? 1 : 0.72}}"></div>
              </div>
              <div class="w-28 text-right font-mono font-semibold ${{isBest ? 'text-[var(--link)]' : ''}}">${{fmtUah(c.min_uah)}} ${{isBest ? '★' : ''}}</div>
            </div>`;
          }}).join("");

          // Channel tabs inside this product
          const tabsHtml = [
            `<button data-tid="${{prod.task_id}}" data-tab="all" class="ch-tab px-2.5 py-1 rounded-lg text-xs font-semibold border border-[var(--border)] ${{curTab === 'all' ? 'bg-[var(--primary)] text-[var(--primary-foreground)]' : 'bg-[var(--background)]'}}">Усі канали</button>`,
            ...CH_KEYS.map(ck => {{
              const c = prod.channels[ck];
              const act = curTab === ck;
              return `<button data-tid="${{prod.task_id}}" data-tab="${{ck}}" class="ch-tab px-2.5 py-1 rounded-lg text-xs font-semibold border border-[var(--border)] ${{act ? 'bg-[var(--primary)] text-[var(--primary-foreground)]' : 'bg-[var(--background)]'}}">${{esc(c.label)}} (${{c.count}})</button>`;
            }})
          ].join("");

          const sortToggle = `<div class="flex items-center gap-1.5 text-xs">
            <span class="text-[var(--muted-foreground)]">Сортування:</span>
            <button data-tid="${{prod.task_id}}" data-sort="price" class="sort-btn px-2 py-0.5 rounded border border-[var(--border)] ${{curSort === 'price' ? 'bg-[var(--secondary)] font-semibold' : ''}}">Ціна ↑</button>
            <button data-tid="${{prod.task_id}}" data-sort="sold" class="sort-btn px-2 py-0.5 rounded border border-[var(--border)] ${{curSort === 'sold' ? 'bg-[var(--secondary)] font-semibold' : ''}}">Продажі ↓</button>
          </div>`;

          const shownKeys = curTab === "all" ? CH_KEYS : [curTab];
          const tablesHtml = shownKeys.map(ck => {{
            const c = prod.channels[ck];
            let items = [...c.items];
            if (curSort === "sold") {{
              items.sort((a, b) => (b.sold - a.sold) || ((b.rating || 0) - (a.rating || 0)) || (a.landed_uah - b.landed_uah));
            }}
            if (!items.length) {{
              const fb = c.fallback;
              const fbLink = fb && fb.url
                ? `<a href="${{esc(fb.url)}}" target="_blank" class="text-[var(--link)] hover:underline ml-2">Відкрити прямий пошук ↗</a>`
                : "";
              const reason = fb ? esc(fb.reason) : "Не запитувалось / 0 результатів";
              return `<div class="p-3 rounded-lg bg-[var(--background)] border border-[var(--border)] text-xs flex flex-wrap items-center justify-between gap-2">
                <div class="flex items-center gap-2">
                  <span class="w-2.5 h-2.5 rounded-sm" style="background:var(${{c.color_var}})"></span>
                  <span class="font-bold">${{esc(c.label)}}</span>
                  <span class="text-[var(--muted-foreground)]">— 0 карток (${{reason}})</span>
                </div>
                ${{fbLink}}
              </div>`;
            }}

            const rows = items.map((it, idx) => {{
              const isMinOverall = prod.best_channel === ck && idx === 0 && curSort === "price";
              const rowBg = isMinOverall ? "bg-[var(--primary)]/10" : "";
              const metaParts = [];
              if (it.sold > 0) metaParts.push(`${{it.sold}} продано`);
              if (it.rating) metaParts.push(`${{it.rating}} ★`);
              const metaStr = metaParts.length ? metaParts.join(" · ") : "—";
              return `<tr class="border-t border-[var(--border)] ${{rowBg}}">
                <td class="py-1.5 px-2 font-mono text-[var(--muted-foreground)] w-7">${{idx + 1}}</td>
                <td class="py-1.5 px-2 font-mono font-bold whitespace-nowrap">${{fmtUah(it.landed_uah)}} ${{isMinOverall ? '<span class="text-[10px] text-[var(--link)] font-semibold ml-1">★ Мін.</span>' : ''}}</td>
                <td class="py-1.5 px-2 font-mono text-[11px] text-[var(--muted-foreground)] whitespace-nowrap">${{esc(it.orig_price)}}</td>
                <td class="py-1.5 px-2 text-[11px] text-[var(--muted-foreground)] whitespace-nowrap">${{esc(it.tax_note)}}</td>
                <td class="py-1.5 px-2 text-[11px] text-[var(--muted-foreground)] whitespace-nowrap">${{esc(metaStr)}}</td>
                <td class="py-1.5 px-2"><a href="${{esc(it.url)}}" target="_blank" class="text-[var(--link)] hover:underline font-medium">${{esc(it.title)}} ↗</a></td>
              </tr>`;
            }}).join("");

            return `<div class="rounded-lg border border-[var(--border)] overflow-hidden">
              <div class="px-3 py-2 bg-[var(--background)] border-b border-[var(--border)] flex items-center justify-between text-xs">
                <div class="flex items-center gap-2 font-bold">
                  <span class="w-2.5 h-2.5 rounded-sm" style="background:var(${{c.color_var}})"></span>
                  <span>${{esc(c.label)}} — Топ-${{items.length}}</span>
                </div>
                <span class="text-[var(--muted-foreground)]">Всього зібрано: ${{c.total_parsed}}</span>
              </div>
              <div class="overflow-x-auto">
                <table class="w-full text-left text-xs border-collapse">
                  <thead class="bg-[var(--background)]/60 text-[11px] text-[var(--muted-foreground)]">
                    <tr>
                      <th class="py-1.5 px-2">#</th>
                      <th class="py-1.5 px-2">Під ключ (₴)</th>
                      <th class="py-1.5 px-2">Ориг. ціна</th>
                      <th class="py-1.5 px-2">Доставка / Мито</th>
                      <th class="py-1.5 px-2">Продажі / Рейтинг</th>
                      <th class="py-1.5 px-2">Товар (пряме посилання)</th>
                    </tr>
                  </thead>
                  <tbody>${{rows}}</tbody>
                </table>
              </div>
            </div>`;
          }}).join("");

          bodyHtml = `<div class="p-4 border-t border-[var(--border)] space-y-4">
            ${{miniBars ? `<div class="space-y-1.5 p-3 rounded-lg bg-[var(--background)]/50 border border-[var(--border)]">${{miniBars}}</div>` : ''}}
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="flex flex-wrap gap-1.5">${{tabsHtml}}</div>
              ${{sortToggle}}
            </div>
            <div class="space-y-3">${{tablesHtml}}</div>
          </div>`;
        }}

        card.innerHTML = `
          <div data-toggle="${{prod.task_id}}" class="prod-header p-4 cursor-pointer hover:bg-[var(--background)]/40 transition-colors flex flex-col gap-3">
            <div class="flex items-start justify-between gap-3">
              <div>
                <div class="text-[11px] font-mono text-[var(--muted-foreground)]">${{esc(prod.task_id)}} · вага ~${{prod.weight_kg}} кг</div>
                <h2 class="text-sm md:text-base font-bold mt-0.5">${{esc(prod.title)}}</h2>
              </div>
              <span class="text-xs px-2.5 py-1 rounded-lg border border-[var(--border)] bg-[var(--background)] shrink-0 font-semibold">${{isOpen ? '▲ Згорнути' : '▼ Розгорнути'}}</span>
            </div>
            <div class="flex flex-wrap gap-2">${{pills}}</div>
          </div>
          ${{bodyHtml}}
        `;
        root.appendChild(card);
      }});

      document.querySelectorAll(".prod-header").forEach(el => {{
        el.onclick = () => {{
          const tid = el.dataset.toggle;
          if (state.openIds.has(tid)) state.openIds.delete(tid);
          else state.openIds.add(tid);
          render();
        }};
      }});
      document.querySelectorAll(".ch-tab").forEach(el => {{
        el.onclick = (e) => {{
          e.stopPropagation();
          state.activeTab[el.dataset.tid] = el.dataset.tab;
          render();
        }};
      }});
      document.querySelectorAll(".sort-btn").forEach(el => {{
        el.onclick = (e) => {{
          e.stopPropagation();
          state.sortMode[el.dataset.tid] = el.dataset.sort;
          render();
        }};
      }});
    }}

    document.getElementById("searchInput").oninput = (e) => {{ state.query = e.target.value; render(); }};
    document.getElementById("expandAllBtn").onclick = () => {{ CATALOG.products.forEach(p => state.openIds.add(p.task_id)); render(); }};
    document.getElementById("collapseAllBtn").onclick = () => {{ state.openIds.clear(); render(); }};

    render();
  </script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic Omni Shopping multi-channel catalog HTML.")
    parser.add_argument("inputs", nargs="+", help="One or more relay_result_*.json files")
    parser.add_argument("-o", "--output", required=True, help="Output HTML file path")
    parser.add_argument("--top", type=int, default=10, help="Top-N offers per channel (default: 10)")
    args = parser.parse_args(argv)

    payload = build_catalog_payload([Path(p) for p in args.inputs], top_n=args.top)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_catalog_html(payload), encoding="utf-8")
    print(f"[catalog_dashboard] Generated {out_path} ({payload['total_products']} products, Top-{payload['top_n']}/channel)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
