/**
 * Omni Shopping Agent — Master Google Sheets Apps Script Bridge
 * Self-bootstrapping spreadsheet setup + JSON Webhook (doGet / doPost).
 *
 * Strict Human-In-The-Loop Guarantee:
 * - Tab `2_Top_Charts_And_Jackpots` writes ONLY columns A:V (22 columns).
 * - Columns W:Y (`Human_Decision`, `Target_Price_UAH`, `Human_Notes`) are preserved
 *   across every re-harvest via `offer_id` (column B) lookup!
 */

const TAB_TASK_QUEUE = "1_Task_Queue";
const TAB_TOP_CHARTS = "2_Top_Charts_And_Jackpots";
const TAB_MANUAL_LINKS = "3_Manual_Check_Links";
const TAB_FUNNEL_AUDIT = "4_Funnel_Audit_And_Quarantine";

const TASK_QUEUE_HEADERS = [
  "task_id",
  "raw_query",
  "canonical_mpns",
  "category",
  "allowed_conditions",
  "max_budget_uah",
  "target_price_uah",
  "urgency_days",
  "estimated_weight_kg",
  "volumetric_factor",
  "customs_mode",
  "status",
  "human_notes",
];

const TOP_CHARTS_HEADERS_A_TO_V = [
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
];

const TOP_CHARTS_HUMAN_HEADERS_W_TO_Y = [
  "Human_Decision",
  "Target_Price_UAH",
  "Human_Notes",
];

const MANUAL_LINKS_HEADERS = [
  "task_id",
  "channel",
  "route_country",
  "reason_status",
  "breakeven_max_foreign_price",
  "currency",
  "breakeven_hint_uk",
  "direct_search_url",
];

const FUNNEL_AUDIT_HEADERS = [
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
];

function _jsonResponse(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON
  );
}

function _verifySecret(secret) {
  const expected = PropertiesService.getScriptProperties().getProperty("OMNI_SYNC_SECRET") || "";
  if (!expected) return true;
  return String(secret || "") === expected;
}

function _ensureSheet(ss, name, headers) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
  }
  const firstRow = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
  const emptyHeader = firstRow.every((cell) => String(cell).trim() === "");
  if (emptyHeader) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.getRange(1, 1, 1, headers.length).setFontWeight("bold");
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function setupMasterSpreadsheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const queueSheet = _ensureSheet(ss, TAB_TASK_QUEUE, TASK_QUEUE_HEADERS);
  if (queueSheet.getLastRow() === 1) {
    queueSheet.appendRow([
      "task_monitor_43_ips",
      'Монітор 43" 4K IPS LG 43SQ700S-W / 43UN700-B',
      "43SQ700S-W, 43UN700-B",
      "monitors_large_format",
      "brand_new, open_box, used_good",
      25000,
      19500,
      7,
      11.5,
      1.2,
      "standard_150eur",
      "PENDING",
      "Перевірити вживані на OLX в Києві (огляд пікселів наживо)",
    ]);
  }

  const allTopHeaders = TOP_CHARTS_HEADERS_A_TO_V.concat(TOP_CHARTS_HUMAN_HEADERS_W_TO_Y);
  const topSheet = _ensureSheet(ss, TAB_TOP_CHARTS, allTopHeaders);
  topSheet.getRange("W1:Y1").setBackground("#FFF3CD").setFontWeight("bold");

  _ensureSheet(ss, TAB_MANUAL_LINKS, MANUAL_LINKS_HEADERS);
  _ensureSheet(ss, TAB_FUNNEL_AUDIT, FUNNEL_AUDIT_HEADERS);
  return { ok: true, spreadsheetId: ss.getId() };
}

function _pullPendingTasks() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = _ensureSheet(ss, TAB_TASK_QUEUE, TASK_QUEUE_HEADERS);
  const lastRow = sheet.getLastRow();
  if (lastRow <= 1) return [];

  const values = sheet.getRange(2, 1, lastRow - 1, TASK_QUEUE_HEADERS.length).getValues();
  const tasks = [];

  for (let i = 0; i < values.length; i++) {
    const r = values[i];
    const status = String(r[11] || "PENDING").trim().toUpperCase();
    if (status !== "PENDING" && status !== "READY") continue;

    const taskId = String(r[0] || "").trim();
    const rawQuery = String(r[1] || "").trim();
    if (!taskId || !rawQuery) continue;

    const mpns = String(r[2] || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const conds = String(r[4] || "brand_new, open_box")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    tasks.push({
      task_id: taskId,
      raw_query: rawQuery,
      canonical_mpns: mpns,
      category: String(r[3] || "general").trim(),
      allowed_conditions: conds.length ? conds : ["brand_new", "open_box"],
      max_budget_uah: r[5] ? Number(r[5]) : null,
      target_price_uah: r[6] ? Number(r[6]) : null,
      urgency_days: r[7] ? Number(r[7]) : 14,
      estimated_weight_kg: r[8] ? Number(r[8]) : 1.5,
      volumetric_factor: r[9] ? Number(r[9]) : 1.0,
      customs_mode: String(r[10] || "standard_150eur").trim(),
      status: status,
      human_notes: String(r[12] || "").trim(),
    });
  }
  return tasks;
}

function _replaceTaskRowsInSimpleSheet(sheet, headers, taskId, rows) {
  const lastRow = sheet.getLastRow();
  const existing = lastRow > 1 ? sheet.getRange(2, 1, lastRow - 1, headers.length).getValues() : [];
  const kept = existing.filter((r) => String(r[0] || "") !== taskId);
  const combined = kept.concat(rows);

  if (lastRow > 1) {
    sheet.getRange(2, 1, lastRow - 1, headers.length).clearContent();
  }
  if (combined.length > 0) {
    sheet.getRange(2, 1, combined.length, headers.length).setValues(combined);
  }
}

function _pushHarvestResults(payload) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const taskId = String(payload.task_id || "");
  if (!taskId) throw new Error("Missing payload.task_id");

  const allTopHeaders = TOP_CHARTS_HEADERS_A_TO_V.concat(TOP_CHARTS_HUMAN_HEADERS_W_TO_Y);
  const topSheet = _ensureSheet(ss, TAB_TOP_CHARTS, allTopHeaders);
  const manualSheet = _ensureSheet(ss, TAB_MANUAL_LINKS, MANUAL_LINKS_HEADERS);
  const auditSheet = _ensureSheet(ss, TAB_FUNNEL_AUDIT, FUNNEL_AUDIT_HEADERS);

  // 1. Preserve human columns W:Y (cols 23..25) by offer_id (col B, index 1)
  const topLastRow = topSheet.getLastRow();
  const humanByOfferId = {};
  const keptOtherTaskRows = [];
  const keptOtherTaskHuman = [];

  if (topLastRow > 1) {
    const existingAtoY = topSheet.getRange(2, 1, topLastRow - 1, 25).getValues();
    for (let i = 0; i < existingAtoY.length; i++) {
      const row = existingAtoY[i];
      const rTaskId = String(row[0] || "");
      const offerId = String(row[1] || "");
      const humanCols = [row[22], row[23], row[24]];
      if (offerId && (humanCols[0] || humanCols[1] || humanCols[2])) {
        humanByOfferId[offerId] = humanCols;
      }
      if (rTaskId !== taskId) {
        keptOtherTaskRows.push(row.slice(0, 22));
        keptOtherTaskHuman.push(humanCols);
      }
    }
  }

  const incomingTop = (payload[TAB_TOP_CHARTS] && payload[TAB_TOP_CHARTS].rows) || [];
  const newTopAtoV = [];
  const newTopWtoY = [];

  for (let i = 0; i < incomingTop.length; i++) {
    const r = incomingTop[i];
    const rowAtoV = r.slice(0, 22);
    while (rowAtoV.length < 22) rowAtoV.push("");
    const offerId = String(rowAtoV[1] || "");
    const preserved = humanByOfferId[offerId] || ["PENDING", "", ""];
    newTopAtoV.push(rowAtoV);
    newTopWtoY.push(preserved);
  }

  const finalAtoV = keptOtherTaskRows.concat(newTopAtoV);
  const finalWtoY = keptOtherTaskHuman.concat(newTopWtoY);

  if (topLastRow > 1) {
    topSheet.getRange(2, 1, topLastRow - 1, 25).clearContent();
  }
  if (finalAtoV.length > 0) {
    // Strictly write cols A:V (22 cols), then restore W:Y (3 cols)
    topSheet.getRange(2, 1, finalAtoV.length, 22).setValues(finalAtoV);
    topSheet.getRange(2, 23, finalWtoY.length, 3).setValues(finalWtoY);
  }

  // 2. Replace rows for task_id in 3_Manual_Check_Links and 4_Funnel_Audit_And_Quarantine
  const incomingLinks = (payload[TAB_MANUAL_LINKS] && payload[TAB_MANUAL_LINKS].rows) || [];
  _replaceTaskRowsInSimpleSheet(manualSheet, MANUAL_LINKS_HEADERS, taskId, incomingLinks);

  const incomingAudit = (payload[TAB_FUNNEL_AUDIT] && payload[TAB_FUNNEL_AUDIT].rows) || [];
  _replaceTaskRowsInSimpleSheet(auditSheet, FUNNEL_AUDIT_HEADERS, taskId, incomingAudit);

  return {
    ok: true,
    task_id: taskId,
    top_rows_written: newTopAtoV.length,
    manual_links_written: incomingLinks.length,
    audit_rows_written: incomingAudit.length,
  };
}

function doGet(e) {
  try {
    const params = (e && e.parameter) || {};
    if (!_verifySecret(params.secret)) {
      return _jsonResponse({ ok: false, error: "UNAUTHORIZED" });
    }
    const action = String(params.action || "pull_pending_tasks");
    if (action === "setup") {
      return _jsonResponse(setupMasterSpreadsheet());
    }
    if (action === "pull_pending_tasks") {
      return _jsonResponse({ ok: true, tasks: _pullPendingTasks() });
    }
    return _jsonResponse({ ok: false, error: "UNKNOWN_ACTION: " + action });
  } catch (err) {
    return _jsonResponse({ ok: false, error: String(err) });
  }
}

function doPost(e) {
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    if (!_verifySecret(body.secret)) {
      return _jsonResponse({ ok: false, error: "UNAUTHORIZED" });
    }
    const action = String(body.action || "");
    if (action === "push_harvest_results") {
      return _jsonResponse(_pushHarvestResults(body.payload || {}));
    }
    if (action === "setup") {
      return _jsonResponse(setupMasterSpreadsheet());
    }
    return _jsonResponse({ ok: false, error: "UNKNOWN_ACTION: " + action });
  } catch (err) {
    return _jsonResponse({ ok: false, error: String(err) });
  }
}
