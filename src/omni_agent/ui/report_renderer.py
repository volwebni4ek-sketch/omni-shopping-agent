from __future__ import annotations

import html as html_lib
import json
from typing import Any


def _esc(val: Any) -> str:
    return html_lib.escape(str(val or ""), quote=True)


def _as_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return obj
    return {}


def _fmt_uah(val: Any) -> str:
    try:
        num = float(val)
        return f"{round(num):,}".replace(",", " ") + " ₴"
    except (TypeError, ValueError):
        return "—"


def _badge_chip(badge: str) -> str:
    colors = {
        "VERIFIED_JACKPOT": ("#065f46", "#d1fae5", "🏆 VERIFIED JACKPOT"),
        "PARETO_OPTIMAL": ("#1e40af", "#dbeafe", "✨ PARETO OPTIMAL"),
        "HUMAN_REVIEW_DEAL": ("#92400e", "#fef3c7", "👀 HUMAN REVIEW DEAL"),
        "STANDARD_VALID": ("#334155", "#f1f5f9", "✅ STANDARD VALID"),
        "QUARANTINE_LOW_TRUST": ("#991b1b", "#fee2e2", "🚧 QUARANTINE"),
        "PRUNED_OUTLIER": ("#64748b", "#f1f5f9", "✂️ PRUNED"),
    }
    fg, bg, label = colors.get(badge, ("#334155", "#f1f5f9", badge or "OFFER"))
    return (
        f'<span class="badge" style="color:{fg};background:{bg};">'
        f"{_esc(label)}</span>"
    )


def _render_offer_card(offer: dict[str, Any], slot_label: str = "") -> str:
    lc = offer.get("landed_cost") or {}
    cond = offer.get("condition") or {}
    specs = offer.get("specs") or {}
    offer_id = _esc(offer.get("offer_id", ""))
    title = _esc(offer.get("title", ""))
    channel = _esc(offer.get("channel", ""))
    route = _esc(offer.get("route_country", "UA"))
    store = _esc(offer.get("store_name", ""))
    url = _esc(offer.get("url", "#"))
    landed = float(lc.get("realistic_landed_uah") or 0.0)
    raw_price = float(offer.get("price_raw") or 0.0)
    currency = _esc(offer.get("currency", "UAH"))
    eff_q = float(offer.get("effective_quality_score") or 0.0)
    ch_score = float(offer.get("channel_score") or 0.0)
    topsis = float(offer.get("topsis_score") or 0.0)
    disc = float(offer.get("discount_vs_median_pct") or 0.0)
    warranty = int(offer.get("warranty_months") or 0)
    delivery = int(offer.get("delivery_days") or 0)
    cond_badge = _esc(cond.get("badge") or cond.get("code") or "")
    tradeoff = _esc(offer.get("tradeoff_summary_uk") or lc.get("notes_uk") or "")
    human_dec = _esc(offer.get("human_decision") or "PENDING")
    inspection_note = _esc(specs.get("inspection_tip_uk") or "")
    panel_type = _esc(specs.get("panel_type") or "")

    disc_html = (
        f'<span class="pill pill-green">-{disc:.1f}% vs медіана</span>'
        if disc >= 5.0
        else (f'<span class="pill pill-gray">+{abs(disc):.1f}% vs медіана</span>' if disc <= -5.0 else "")
    )
    slot_html = f'<div class="slot-label">{_esc(slot_label)}</div>' if slot_label else ""
    panel_html = f'<span class="pill pill-blue">{panel_type}</span>' if panel_type else ""
    inspect_html = (
        f'<div class="inspect-note">🔍 {_esc(specs.get("inspection_tip_uk"))}</div>'
        if inspection_note
        else ""
    )

    return f"""
    <article class="card offer-card"
             data-offer-id="{offer_id}"
             data-price="{landed:.2f}"
             data-quality="{eff_q:.2f}"
             data-channel="{ch_score:.2f}"
             data-topsis="{topsis:.4f}"
             data-route="{route}"
             data-cond="{_esc(cond.get('code', ''))}">
      {slot_html}
      <div class="card-top">
        {_badge_chip(str(offer.get("deal_badge", "")))}
        <span class="pill">{channel} · {route}</span>
        {panel_html}
        {disc_html}
      </div>
      <h3 class="offer-title"><a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a></h3>
      <div class="meta-line">
        <span>🏪 {store}</span>
        <span>{cond_badge}</span>
        <span>🚚 {delivery} дн.</span>
        <span>🛡️ {warranty} міс. гарантії</span>
      </div>
      <div class="price-row">
        <div>
          <div class="price-main">{_fmt_uah(landed)}</div>
          <div class="price-sub">Ціна продавця: {raw_price:,.2f} {currency}</div>
        </div>
        <div class="scores-box">
          <div>TOPSIS: <strong class="dyn-topsis">{topsis:.3f}</strong></div>
          <div>Якість: <strong>{eff_q:.1f}</strong>/100</div>
          <div>Канал: <strong>{ch_score:.0f}</strong>/100</div>
        </div>
      </div>
      <p class="tradeoff-text">{tradeoff}</p>
      {inspect_html}
      <div class="decision-bar" data-offer-id="{offer_id}">
        <span class="dec-status">Рішення: <strong class="dec-label">{human_dec}</strong></span>
        <div class="dec-btns">
          <button type="button" class="btn btn-approve" onclick="setDecision('{offer_id}', 'APPROVE')">✅ Обрати</button>
          <button type="button" class="btn btn-watch" onclick="setDecision('{offer_id}', 'WATCH')">👀 Слідкувати</button>
          <button type="button" class="btn btn-reject" onclick="setDecision('{offer_id}', 'REJECT')">❌ Відхилити</button>
        </div>
      </div>
    </article>
    """


def render_standalone_hitl_html(bundle: dict[str, Any]) -> str:
    """
    Renders a self-contained responsive HTML HITL Portal with 4 zones:
      - Zone A: Top-Chart Quartet & Verified Jackpots + live JS TOPSIS preference sliders + Hard Trade-Off Cards
      - Zone B: Human Review Deals (OLX Доставка / Б/В вживу, Prom-оплата, Уцінка / Open-Box)
      - Zone C: Direct Manual Check Links (Ukrainian OLX/Prom/Rozetka/Telemart/CompBest + EU/US) + interactive calculator
      - Zone D: Funnel & IQR Audit Trail + Quarantine & Pruned Outliers
    """
    task = _as_dict(bundle.get("task") or {})
    task_id = str(bundle.get("task_id") or task.get("task_id") or "omni_task")
    raw_query = str(task.get("raw_query") or task_id)
    top_chart = bundle.get("top_chart") or {}
    quartet = top_chart.get("quartet") or {}
    dilemmas = top_chart.get("hard_tradeoff_dilemmas") or []
    audit = _as_dict(bundle.get("audit") or {})
    ranked_offers = [_as_dict(o) for o in (bundle.get("ranked_offers") or [])]
    human_review = [_as_dict(o) for o in (bundle.get("human_review_deals") or [])]
    manual_links = [_as_dict(l) for l in (bundle.get("manual_links") or [])]
    quarantine_offers = [_as_dict(o) for o in (bundle.get("quarantine_offers") or audit.get("quarantine_offers") or [])]
    pruned_offers = [_as_dict(o) for o in (bundle.get("pruned_offers") or audit.get("pruned_offers") or [])]
    alerts = bundle.get("alerts") or []
    llm_review = _as_dict(bundle.get("llm_review") or {})
    briefing_md = str(bundle.get("briefing_md") or "")

    slot_titles = {
        "best_overall_topsis": "🥇 Лідер рейтингу TOPSIS (Найкращий баланс)",
        "verified_jackpot_or_import": "💎 Джекпот / Найкращий імпорт або спецціна",
        "best_buy_today_ua": "⚡ Найкраща покупка сьогодні в Україні",
        "smart_open_box_or_review": "🔍 Розумний Open-Box / Б/В (перевірка вживу)",
    }

    quartet_cards_html = ""
    for slot_key, slot_label in slot_titles.items():
        slot_offer = _as_dict(quartet.get(slot_key))
        if slot_offer:
            quartet_cards_html += _render_offer_card(slot_offer, slot_label=slot_label)

    if not quartet_cards_html:
        quartet_cards_html = '<div class="empty-box">Не знайдено жодного кандидата у Top-Chart Quartet.</div>'

    ranked_cards_html = "".join(_render_offer_card(o) for o in ranked_offers)
    if not ranked_cards_html:
        ranked_cards_html = '<div class="empty-box">Список валідних пропозицій порожній.</div>'

    dilemmas_html = ""
    for d in dilemmas:
        d_dict = _as_dict(d)
        dilemmas_html += f"""
        <div class="dilemma-card">
          <div class="dilemma-title">⚖️ Жорсткий компроміс: економія {_fmt_uah(d_dict.get("savings_uah"))} (-{float(d_dict.get("savings_pct") or 0):.1f}%)</div>
          <div class="dilemma-body">{_esc(d_dict.get("dilemma_uk", ""))}</div>
          <div class="dilemma-sub">
            <span>База UA: {_esc(d_dict.get("baseline_ua_title", ""))} ({_fmt_uah(d_dict.get("baseline_ua_price_uah"))})</span>
            <span>Альтернатива: {_esc(d_dict.get("challenger_title", ""))} ({_fmt_uah(d_dict.get("challenger_price_uah"))})</span>
          </div>
        </div>
        """
    if not dilemmas_html:
        dilemmas_html = '<div class="empty-box">Жорстких компромісів з економією ≥15% не виявлено — локальний офіційний варіант вже конкурентний.</div>'

    human_review_html = "".join(_render_offer_card(o, slot_label="🛡️ Потрібна ручна перевірка / Живий огляд") for o in human_review)
    if not human_review_html:
        human_review_html = '<div class="empty-box">У цій сесії немає аномальних пропозицій Tier 2 (OLX/Prom) на ручну перевірку.</div>'

    # Split Manual Check Links into UA Secondary/Outlet vs EU/US Import
    ua_links_rows = ""
    global_links_rows = ""
    for link in manual_links:
        ch = _esc(link.get("channel", ""))
        rc = _esc(link.get("route_country", "UA"))
        reason = _esc(link.get("reason_status", ""))
        cur = _esc(link.get("currency", "UAH"))
        be_val = link.get("breakeven_max_foreign_price")
        be_txt = f"до {float(be_val):,.2f} {cur}" if be_val is not None else "—"
        hint = _esc(link.get("breakeven_hint_uk", ""))
        url = _esc(link.get("direct_search_url", "#"))
        row_html = f"""
        <tr>
          <td><strong>{ch}</strong></td>
          <td><span class="pill">{rc}</span></td>
          <td><code>{reason}</code></td>
          <td><strong>{be_txt}</strong></td>
          <td>{hint}</td>
          <td><a class="btn-link" href="{url}" target="_blank" rel="noopener noreferrer">Відкрити пошук ↗</a></td>
        </tr>
        """
        if rc == "UA":
            ua_links_rows += row_html
        else:
            global_links_rows += row_html

    quarantine_rows = ""
    for q in quarantine_offers:
        lc = q.get("landed_cost") or {}
        quarantine_rows += f"""
        <tr>
          <td>{_badge_chip(str(q.get("deal_badge", "")))}</td>
          <td>{_esc(q.get("channel", ""))}</td>
          <td><a href="{_esc(q.get('url', '#'))}" target="_blank" rel="noopener noreferrer">{_esc(q.get("title", ""))}</a></td>
          <td>{_fmt_uah(lc.get("realistic_landed_uah"))}</td>
          <td>{_esc(q.get("prune_reason", ""))}</td>
        </tr>
        """

    pruned_rows = ""
    for p in pruned_offers:
        lc = p.get("landed_cost") or {}
        pruned_rows += f"""
        <tr>
          <td>{_badge_chip(str(p.get("deal_badge", "")))}</td>
          <td>{_esc(p.get("channel", ""))}</td>
          <td>{_esc(p.get("title", ""))}</td>
          <td>{_fmt_uah(lc.get("realistic_landed_uah"))}</td>
          <td>{_esc(p.get("prune_reason", ""))}</td>
        </tr>
        """

    alerts_html = ""
    if alerts:
        items = "".join(f"<li>🔔 {_esc(a.get('message_uk', ''))}</li>" for a in alerts)
        alerts_html = f'<section class="alert-banner"><strong>Алерти зниження цін:</strong><ul>{items}</ul></section>'

    llm_summary_html = ""
    if llm_review.get("summary_uk"):
        llm_summary_html = (
            f'<div class="llm-box"><strong>🤖 Інженерний аудит ({_esc(llm_review.get("source", "local"))}):</strong> '
            f'{_esc(llm_review.get("summary_uk"))}</div>'
        )

    med_uah = float(audit.get("reference_median_uah") or 0.0)
    iqr_uah = float(audit.get("iqr_uah") or 0.0)
    q1_uah = float(audit.get("iqr_q1_uah") or 0.0)
    q3_uah = float(audit.get("iqr_q3_uah") or 0.0)
    total_raw = int(audit.get("total_raw_offers") or 0)
    after_sieve = int(audit.get("after_semantic_sieve") or 0)
    summary_uk = _esc(audit.get("pruned_summary_uk") or "Аудит воронкою виконано.")
    weight_kg = float(task.get("estimated_weight_kg") or 1.5)
    customs_mode = str(task.get("customs_mode") or "standard_150eur")

    return f"""<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Omni Shopping Agent — HITL Portal: {_esc(raw_query)}</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --surface: #ffffff;
      --border: #e2e8f0;
      --text: #0f172a;
      --muted: #475569;
      --primary: #2563eb;
      --green: #059669;
      --amber: #d97706;
      --red: #dc2626;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.5;
    }}
    .container {{ max-width: 1320px; margin: 0 auto; padding: 20px; }}
    header.hero {{
      background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 100%);
      color: #fff; border-radius: 14px; padding: 22px 26px; margin-bottom: 20px;
    }}
    header.hero h1 {{ margin: 0 0 8px 0; font-size: 1.5rem; }}
    .hero-meta {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 0.9rem; opacity: 0.92; }}
    .kpi-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 12px; margin-bottom: 20px;
    }}
    .kpi {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 10px; padding: 14px 16px;
    }}
    .kpi-label {{ font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}
    .kpi-val {{ font-size: 1.3rem; font-weight: 700; margin-top: 4px; }}
    section.zone {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 14px; padding: 20px; margin-bottom: 20px;
    }}
    section.zone h2 {{ margin: 0 0 12px 0; font-size: 1.25rem; border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
    .sliders-panel {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px; background: #f1f5f9; border-radius: 10px; padding: 14px; margin-bottom: 16px;
    }}
    .slider-item label {{ display: flex; justify-content: space-between; font-weight: 600; font-size: 0.9rem; }}
    .slider-item input[type="range"] {{ width: 100%; margin-top: 6px; }}
    .cards-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
      gap: 14px;
    }}
    .card {{
      border: 1px solid var(--border); border-radius: 12px; padding: 16px;
      background: #fff; display: flex; flex-direction: column; gap: 10px;
      box-shadow: 0 1px 2px rgba(15,23,42,0.04);
    }}
    .slot-label {{
      font-size: 0.8rem; font-weight: 700; color: var(--primary);
      background: #eff6ff; padding: 4px 10px; border-radius: 999px; align-self: flex-start;
    }}
    .card-top {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
    .badge {{ font-size: 0.75rem; font-weight: 700; padding: 3px 8px; border-radius: 6px; }}
    .pill {{ font-size: 0.75rem; padding: 2px 8px; border-radius: 999px; background: #f1f5f9; color: #334155; }}
    .pill-green {{ background: #dcfce7; color: #166534; font-weight: 600; }}
    .pill-blue {{ background: #dbeafe; color: #1e40af; font-weight: 600; }}
    .pill-gray {{ background: #e2e8f0; color: #475569; }}
    .offer-title {{ margin: 0; font-size: 1rem; }}
    .offer-title a {{ color: var(--text); text-decoration: none; }}
    .offer-title a:hover {{ color: var(--primary); text-decoration: underline; }}
    .meta-line {{ display: flex; flex-wrap: wrap; gap: 10px; font-size: 0.82rem; color: var(--muted); }}
    .price-row {{
      display: flex; justify-content: space-between; align-items: center;
      background: #f8fafc; border-radius: 8px; padding: 10px 12px;
    }}
    .price-main {{ font-size: 1.35rem; font-weight: 800; color: var(--text); }}
    .price-sub {{ font-size: 0.78rem; color: var(--muted); }}
    .scores-box {{ text-align: right; font-size: 0.82rem; color: var(--muted); }}
    .tradeoff-text {{ margin: 0; font-size: 0.84rem; color: var(--muted); }}
    .inspect-note {{
      font-size: 0.82rem; background: #fefce8; border: 1px solid #fde047;
      color: #854d0e; border-radius: 8px; padding: 7px 10px;
    }}
    .decision-bar {{
      display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
      gap: 8px; border-top: 1px solid var(--border); padding-top: 10px; margin-top: auto;
    }}
    .dec-status {{ font-size: 0.82rem; }}
    .dec-btns {{ display: flex; gap: 6px; }}
    .btn {{
      border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px;
      font-size: 0.78rem; cursor: pointer; background: #fff; font-weight: 600;
    }}
    .btn:hover {{ background: #f1f5f9; }}
    .btn-approve {{ border-color: #86efac; color: #166534; }}
    .btn-watch {{ border-color: #fcd34d; color: #92400e; }}
    .btn-reject {{ border-color: #fca5a5; color: #991b1b; }}
    .dilemma-card {{
      border: 1px solid #fed7aa; background: #fff7ed; border-radius: 10px;
      padding: 12px 14px; margin-bottom: 10px;
    }}
    .dilemma-title {{ font-weight: 700; color: #9a3412; margin-bottom: 4px; }}
    .dilemma-body {{ font-size: 0.92rem; }}
    .dilemma-sub {{ display: flex; flex-wrap: wrap; gap: 16px; margin-top: 6px; font-size: 0.8rem; color: #7c2d12; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.86rem; margin-top: 8px; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
    th {{ background: #f8fafc; font-weight: 700; }}
    .btn-link {{
      display: inline-block; padding: 4px 10px; border-radius: 6px;
      background: var(--primary); color: #fff; text-decoration: none; font-weight: 600; font-size: 0.8rem;
      white-space: nowrap;
    }}
    .calc-box {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px; background: #f1f5f9; padding: 14px; border-radius: 10px; margin-top: 14px;
    }}
    .calc-box input, .calc-box select {{
      width: 100%; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 0.9rem;
    }}
    .alert-banner {{
      background: #ecfdf5; border: 1px solid #6ee7b7; color: #065f46;
      border-radius: 10px; padding: 12px 16px; margin-bottom: 16px;
    }}
    .llm-box {{
      background: #eef2ff; border: 1px solid #c7d2fe; color: #312e81;
      border-radius: 10px; padding: 12px 14px; margin-bottom: 16px; font-size: 0.92rem;
    }}
    .empty-box {{ padding: 14px; background: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 8px; color: var(--muted); }}
    .sub-heading {{ margin: 16px 0 8px 0; font-size: 1rem; color: var(--text); }}
    pre.briefing-pre {{
      background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 10px;
      overflow-x: auto; font-size: 0.82rem; white-space: pre-wrap;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header class="hero">
      <h1>🛒 Omni Shopping Agent — HITL Portal</h1>
      <div class="hero-meta">
        <span><strong>Задача:</strong> {_esc(raw_query)}</span>
        <span><strong>ID:</strong> <code>{_esc(task_id)}</code></span>
        <span><strong>Категорія:</strong> {_esc(task.get("category", "general"))}</span>
        <span><strong>Вага:</strong> {weight_kg:.1f} кг</span>
        <span><strong>Митниця:</strong> {_esc(customs_mode)}</span>
      </div>
    </header>

    {alerts_html}
    {llm_summary_html}

    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-label">Всього зібрано</div><div class="kpi-val">{total_raw} (сито: {after_sieve})</div></div>
      <div class="kpi"><div class="kpi-label">Опорна медіана UA</div><div class="kpi-val">{_fmt_uah(med_uah)}</div></div>
      <div class="kpi"><div class="kpi-label">IQR Коридор (Q1–Q3)</div><div class="kpi-val">{_fmt_uah(q1_uah)} – {_fmt_uah(q3_uah)}</div></div>
      <div class="kpi"><div class="kpi-label">Розкид IQR</div><div class="kpi-val">{_fmt_uah(iqr_uah)}</div></div>
    </div>

    <!-- ZONE A -->
    <section class="zone" id="zone-a">
      <h2>Zone A · Top-Chart Quartet, Верифіковані Джекпоти &amp; Live TOPSIS Баланс</h2>
      <div class="sliders-panel">
        <div class="slider-item">
          <label for="wPrice">💰 Ціна (Економія) <span id="wPriceVal">35%</span></label>
          <input id="wPrice" type="range" min="5" max="80" value="35" oninput="recomputeTopsis()" />
        </div>
        <div class="slider-item">
          <label for="wQuality">👁️ Якість &amp; Здоров'я очей (IPS/Eye-Care) <span id="wQualityVal">40%</span></label>
          <input id="wQuality" type="range" min="5" max="80" value="40" oninput="recomputeTopsis()" />
        </div>
        <div class="slider-item">
          <label for="wChannel">🛡️ Швидкість &amp; Живий огляд / Гарантія УА <span id="wChannelVal">25%</span></label>
          <input id="wChannel" type="range" min="5" max="80" value="25" oninput="recomputeTopsis()" />
        </div>
      </div>

      <h3 class="sub-heading">Квартет переможців</h3>
      <div class="cards-grid">{quartet_cards_html}</div>

      <h3 class="sub-heading">⚖️ Hard Trade-Off Cards (Новий офіціал vs Імпорт / Open-Box / OLX Б/В вживу)</h3>
      {dilemmas_html}

      <h3 class="sub-heading">Повний рейтинг кандидатів (Live пересортування за слайдерами)</h3>
      <div class="cards-grid" id="ranked-list">{ranked_cards_html}</div>
    </section>

    <!-- ZONE B -->
    <section class="zone" id="zone-b">
      <h2>Zone B · Human Review Deals (OLX Доставка / Б/В вживу, Prom-оплата, Уцінка / Open-Box)</h2>
      <div class="cards-grid">{human_review_html}</div>
    </section>

    <!-- ZONE C -->
    <section class="zone" id="zone-c">
      <h2>Zone C · Direct Manual Check Links &amp; Калькулятор окупності</h2>
      <h3 class="sub-heading">🇺🇦 Український локальний / вторинний ринок та уцінка (живий огляд пікселів і матриці)</h3>
      <table>
        <thead>
          <tr><th>Канал</th><th>Країна</th><th>Статус</th><th>Орієнтир ціни</th><th>Підказка</th><th>Дія</th></tr>
        </thead>
        <tbody>{ua_links_rows or '<tr><td colspan="6">Немає локальних посилань.</td></tr>'}</tbody>
      </table>

      <h3 class="sub-heading">🌍 Закордонні та антибот-захищені канали (EU / US / AliExpress)</h3>
      <table>
        <thead>
          <tr><th>Канал</th><th>Країна</th><th>Статус</th><th>Breakeven макс. ціна</th><th>Розрахунок точки беззбитковості</th><th>Дія</th></tr>
        </thead>
        <tbody>{global_links_rows or '<tr><td colspan="6">Закордонні посилання приховано (--ua-only режим) або не потрібні.</td></tr>'}</tbody>
      </table>

      <h3 class="sub-heading">🧮 Інтерактивний калькулятор точки беззбитковості / Landed Cost</h3>
      <div class="calc-box">
        <div>
          <label>Ціна товару (валюта)</label>
          <input id="calcPrice" type="number" value="400" step="1" oninput="updateCalc()" />
        </div>
        <div>
          <label>Маршрут / Валюта</label>
          <select id="calcRoute" onchange="updateCalc()">
            <option value="DE_DIRECT">Німеччина (Amazon DE Direct -19% VAT, EUR)</option>
            <option value="DE_FW">Німеччина (Meest/NP форвардер, EUR)</option>
            <option value="PL_FW">Польща (Ceneo/Allegro форвардер, PLN)</option>
            <option value="US_FW">США (Amazon/eBay форвардер, USD)</option>
            <option value="UA_LOCAL">Україна (OLX/Prom ескроу, UAH)</option>
          </select>
        </div>
        <div>
          <label>Вага (кг)</label>
          <input id="calcWeight" type="number" value="{weight_kg:.1f}" step="0.5" oninput="updateCalc()" />
        </div>
        <div>
          <label>Підсумкова ціна в Україні (Landed UAH)</label>
          <div id="calcOutput" style="font-size:1.25rem;font-weight:800;padding-top:6px;">—</div>
        </div>
      </div>
    </section>

    <!-- ZONE D -->
    <section class="zone" id="zone-d">
      <h2>Zone D · Funnel &amp; IQR Audit Trail, Quarantine та Відсіяні пастки</h2>
      <p><strong>Підсумок воронки:</strong> {summary_uk}</p>

      <h3 class="sub-heading">🚧 Карантин (Tier 3 або підозріло низька ціна &lt;25% медіани)</h3>
      <table>
        <thead><tr><th>Статус</th><th>Канал</th><th>Назва</th><th>Landed ₴</th><th>Причина карантину</th></tr></thead>
        <tbody>{quarantine_rows or '<tr><td colspan="5">Карантин порожній.</td></tr>'}</tbody>
      </table>

      <h3 class="sub-heading">✂️ Відсіяні на ситі (аксесуари, биті матриці, kit traps, завищена ціна IQR)</h3>
      <table>
        <thead><tr><th>Статус</th><th>Канал</th><th>Назва</th><th>Landed ₴</th><th>Причина відсіву</th></tr></thead>
        <tbody>{pruned_rows or '<tr><td colspan="5">Відсіяних позицій немає.</td></tr>'}</tbody>
      </table>

      {f'<h3 class="sub-heading">📝 Market Briefing (Markdown)</h3><pre class="briefing-pre">{_esc(briefing_md)}</pre>' if briefing_md else ''}
    </section>
  </div>

  <script>
    const CUSTOMS_MODE = {json.dumps(customs_mode)};

    function setDecision(offerId, decision) {{
      const bars = document.querySelectorAll('.decision-bar[data-offer-id="' + offerId + '"]');
      bars.forEach((bar) => {{
        const lbl = bar.querySelector('.dec-label');
        if (lbl) lbl.textContent = decision;
      }});
      try {{
        localStorage.setItem('omni_dec_' + offerId, decision);
      }} catch (e) {{}}

      if (window.location.protocol.startsWith('http')) {{
        fetch('/api/decisions', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ offer_id: offerId, decision: decision }})
        }}).catch(() => {{}});
      }}
    }}

    function recomputeTopsis() {{
      const wpRaw = Number(document.getElementById('wPrice').value);
      const wqRaw = Number(document.getElementById('wQuality').value);
      const wcRaw = Number(document.getElementById('wChannel').value);
      const sum = wpRaw + wqRaw + wcRaw || 1;
      const wp = wpRaw / sum, wq = wqRaw / sum, wc = wcRaw / sum;

      document.getElementById('wPriceVal').textContent = Math.round(wp * 100) + '%';
      document.getElementById('wQualityVal').textContent = Math.round(wq * 100) + '%';
      document.getElementById('wChannelVal').textContent = Math.round(wc * 100) + '%';

      const container = document.getElementById('ranked-list');
      if (!container) return;
      const cards = Array.from(container.querySelectorAll('.offer-card'));
      if (cards.length < 2) return;

      const prices = cards.map(c => Math.max(1, Number(c.dataset.price || 1)));
      const quals = cards.map(c => Math.max(0, Number(c.dataset.quality || 0)));
      const chans = cards.map(c => Math.max(0, Number(c.dataset.channel || 0)));

      const norm = (arr) => Math.sqrt(arr.reduce((acc, v) => acc + v * v, 0)) || 1;
      const dp = norm(prices), dq = norm(quals), dc = norm(chans);

      const vp = prices.map(v => (v / dp) * wp);
      const vq = quals.map(v => (v / dq) * wq);
      const vc = chans.map(v => (v / dc) * wc);

      const bp = Math.min(...vp), wpWorst = Math.max(...vp);
      const bq = Math.max(...vq), wqWorst = Math.min(...vq);
      const bc = Math.max(...vc), wcWorst = Math.min(...vc);

      cards.forEach((card, i) => {{
        const dBest = Math.hypot(vp[i] - bp, vq[i] - bq, vc[i] - bc);
        const dWorst = Math.hypot(vp[i] - wpWorst, vq[i] - wqWorst, vc[i] - wcWorst);
        const score = (dBest + dWorst) > 0 ? dWorst / (dBest + dWorst) : 0.5;
        card.dataset.liveTopsis = score.toFixed(4);
        const el = card.querySelector('.dyn-topsis');
        if (el) el.textContent = score.toFixed(3);
      }});

      cards.sort((a, b) => Number(b.dataset.liveTopsis) - Number(a.dataset.liveTopsis));
      cards.forEach(c => container.appendChild(c));
    }}

    function updateCalc() {{
      const price = Math.max(0, Number(document.getElementById('calcPrice').value || 0));
      const route = document.getElementById('calcRoute').value;
      const weight = Math.max(0.5, Number(document.getElementById('calcWeight').value || 1.5));
      const EUR = 45.0, USD = 41.5, PLN = 10.5;
      let netUah = price;
      let fwUah = 0;
      let fxPct = 0.01;

      if (route === 'DE_DIRECT') {{
        netUah = (price * EUR) / 1.19 + 25 * EUR;
      }} else if (route === 'DE_FW') {{
        netUah = price * EUR;
        fwUah = (3.5 + 3.8 * weight) * USD;
      }} else if (route === 'PL_FW') {{
        netUah = price * PLN;
        fwUah = (2.5 + 3.0 * weight) * USD;
        fxPct = 0.02;
      }} else if (route === 'US_FW') {{
        netUah = price * USD;
        fwUah = (5.0 + 6.5 * weight) * USD;
      }} else {{
        netUah = price + Math.max(120, price * 0.015 + 100);
        fxPct = 0.0;
      }}

      let customs = 0;
      let broker = 0;
      if (route !== 'UA_LOCAL' && CUSTOMS_MODE === 'standard_150eur') {{
        const netEur = netUah / EUR;
        if (netEur > 150) {{
          const excess = (netEur - 150) * EUR;
          const duty = excess * 0.10;
          const vat = (excess + duty) * 0.20;
          customs = duty + vat;
          broker = 350;
        }}
      }}
      const total = netUah + fwUah + customs + broker + (netUah + fwUah) * fxPct;
      document.getElementById('calcOutput').textContent = Math.round(total).toLocaleString('uk-UA') + ' ₴';
    }}

    document.addEventListener('DOMContentLoaded', () => {{
      document.querySelectorAll('.decision-bar[data-offer-id]').forEach((bar) => {{
        const oid = bar.dataset.offerId;
        try {{
          const saved = localStorage.getItem('omni_dec_' + oid);
          if (saved) {{
            const lbl = bar.querySelector('.dec-label');
            if (lbl && lbl.textContent === 'PENDING') lbl.textContent = saved;
          }}
        }} catch (e) {{}}
      }});
      updateCalc();
    }});
  </script>
</body>
</html>
"""
