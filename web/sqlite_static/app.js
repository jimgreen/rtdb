const el = (id) => document.getElementById(id);
const state = { table: null, tables: [], definition: null, version: null, records: [], total: 0, tableTotal: 0,
  offset: 0, fields: new Set(), selected: new Set(), checkedTables: new Set(), logs: [], history: [],
  busy: false, schemaDirty: false, timer: null, dialog: null, chartFields: new Set(), lastQuery: null,
  resultColumns: [], editable: false, pendingEdits: new Map(), draftSequence: 0, copiedRows: null, addRowIndex: null, selectedColumns: new Set(), selectedCells: new Set(),
  rowAnchor: null, columnAnchor: null, cellAnchor: null, cellDrag: null, chartMode: "rows", chartAxes: new Map(),
  chartFrame: null, chartHits: [], chartCursor: { index: -1, pinned: false, y: 0 }, chartDrag: null, contextRecord: null,
  chartSeries: [], chartPinned: false, chartCleared: true, chartQueryKey: null, chartHidden: new Set(),
  chartStyles: new Map(), chartTitle: "", chartTitleSettings: null, auditRecords: [], auditLoaded: false, logQuery: { date: "", keyword: "", category: "", status: "", page: 1, pageSize: 50, total: 0, pages: 1 },
  logConfig: null, logDates: [], databaseFiles: [], chartAxisSettings: {
    left: { title: "左轴", min: null, max: null, showGrid: true },
    right: { title: "右轴", min: null, max: null, showGrid: true },
    x: { title: "", labelStep: 1 }
  }, chartSettingsDialog: null, historyScope: "", dataDir: "", schemaContextRow: null, databaseRepairFile: null, sidebarContextTrigger: null, sidebarContextScope: null };
let sqlHistoryItems = [], sqlHistoryActive = -1;
const integerTypes = new Set(["int", "integer", "long"]);
const floatTypes = new Set(["float", "double", "number", "decimal"]);
const boolTypes = new Set(["bool", "boolean"]);
const objectTypes = new Set(["list", "array", "dict", "object", "json"]);
const colors = ["#2869d5", "#19a186", "#e3922e", "#9764d0", "#dc607b", "#268eac"];
const chartSettingsStorageKey = "rtdb.chart.settings";
const chartTitleDefaults = { color: "#314c70", fontSize: 13, fontFamily: "Segoe UI", fontStyle: "bold", position: "top-center", gap: 6 };
const chartTitleFonts = ["Segoe UI", "Microsoft YaHei", "SimSun", "SimHei", "Arial"];
const chartTitleStyles = ["normal", "bold", "italic", "bold-italic"];
const chartTitlePositions = ["top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"];
function normalizeChartTitleSettings(value) {
  const settings = value && typeof value === "object" ? value : {}, bounded = (key, min, max) => typeof settings[key] === "number" && Number.isFinite(settings[key]) ? Math.max(min, Math.min(max, Math.round(settings[key]))) : chartTitleDefaults[key];
  return {
    color: /^#[0-9a-f]{6}$/i.test(settings.color) ? settings.color : chartTitleDefaults.color,
    fontSize: bounded("fontSize", 8, 72), gap: bounded("gap", 0, 32),
    fontFamily: chartTitleFonts.includes(settings.fontFamily) ? settings.fontFamily : chartTitleDefaults.fontFamily,
    fontStyle: chartTitleStyles.includes(settings.fontStyle) ? settings.fontStyle : chartTitleDefaults.fontStyle,
    position: chartTitlePositions.includes(settings.position) ? settings.position : chartTitleDefaults.position
  };
}
state.chartTitleSettings = normalizeChartTitleSettings();
function loadChartSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(chartSettingsStorageKey) || "null");
    if (!saved || typeof saved !== "object") return;
    if (typeof saved.title === "string") state.chartTitle = saved.title;
    state.chartTitleSettings = normalizeChartTitleSettings(saved.titleSettings);
    for (const axis of ["left", "right", "x"]) {
      if (!saved.axes?.[axis] || typeof saved.axes[axis] !== "object") continue;
      Object.assign(state.chartAxisSettings[axis], saved.axes[axis]);
    }
    for (const [key, style] of Object.entries(saved.styles || {})) {
      if (style && typeof style === "object") state.chartStyles.set(key, style);
    }
  } catch { /* Optional preference persistence. */ }
}
function saveChartSettings() {
  try {
    localStorage.setItem(chartSettingsStorageKey, JSON.stringify({
      title: state.chartTitle, titleSettings: state.chartTitleSettings, axes: state.chartAxisSettings, styles: Object.fromEntries(state.chartStyles)
    }));
  } catch { /* Optional preference persistence. */ }
}
loadChartSettings();
el("chart-mode").value = state.chartMode;

function setupSplitter({ id, panelId, axis, defaultSize, minimum, maximum, collapseThreshold, cssVariable, collapsedClass, direction = 1 }) {
  const splitter = el(id), panel = el(panelId), root = document.documentElement;
  const storageKey = `rtdb.layout.${id}`;
  let value = defaultSize, drag = null;
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved !== null && saved.trim() !== "" && Number.isFinite(Number(saved)) && Number(saved) >= 0) value = Number(saved);
  } catch { /* Layout remains usable when browser storage is unavailable. */ }
  const bounds = () => {
    const min = minimum();
    return { min, max: Math.max(min, maximum()) };
  };
  const save = () => { try { localStorage.setItem(storageKey, String(value)); } catch { /* Optional persistence. */ } };
  const apply = (next) => {
    const { min, max } = bounds();
    value = next <= collapseThreshold ? 0 : Math.round(Math.max(min, Math.min(max, next)));
    if (value === 0 && panel.contains(document.activeElement)) splitter.focus({ preventScroll: true });
    root.style.setProperty(cssVariable, `${value}px`);
    root.classList.toggle(collapsedClass, value === 0);
    panel.inert = value === 0;
    splitter.setAttribute("aria-valuemin", "0");
    splitter.setAttribute("aria-valuemax", String(Math.round(max)));
    splitter.setAttribute("aria-valuenow", String(value));
    splitter.setAttribute("aria-valuetext", value === 0 ? "已收起，向外拖拽展开" : `${value} 像素`);
  };
  const position = (event) => direction * (axis === "x" ? event.clientX : event.clientY);
  const finish = (commit) => {
    if (!drag) return;
    const previous = drag;
    drag = null;
    if (commit) save(); else apply(previous.initial);
    splitter.classList.remove("is-dragging");
    root.classList.remove(`resizing-${axis}`);
    if (splitter.hasPointerCapture(previous.pointerId)) splitter.releasePointerCapture(previous.pointerId);
  };
  splitter.addEventListener("pointerdown", (event) => {
    if (drag || !event.isPrimary || event.button !== 0) return;
    event.preventDefault();
    splitter.focus({ preventScroll: true });
    splitter.setPointerCapture(event.pointerId);
    drag = { pointerId: event.pointerId, start: position(event), initial: value };
    splitter.classList.add("is-dragging");
    root.classList.add(`resizing-${axis}`);
  });
  splitter.addEventListener("pointermove", (event) => {
    if (drag?.pointerId === event.pointerId) apply(drag.initial + position(event) - drag.start);
  });
  splitter.addEventListener("pointerup", (event) => {
    if (drag?.pointerId !== event.pointerId) return;
    apply(drag.initial + position(event) - drag.start);
    finish(true);
  });
  splitter.addEventListener("pointercancel", (event) => { if (drag?.pointerId === event.pointerId) finish(false); });
  splitter.addEventListener("lostpointercapture", () => finish(false));
  window.addEventListener("blur", () => finish(false));
  splitter.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && drag) { event.preventDefault(); finish(false); return; }
    if (drag) return;
    const decrease = axis === "x" ? "ArrowLeft" : "ArrowUp", increase = axis === "x" ? "ArrowRight" : "ArrowDown";
    if (![decrease, increase, "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const { min, max } = bounds(), step = axis === "x" ? 16 : 8;
    if (event.key === "Home") apply(0);
    else if (event.key === "End") apply(max);
    else if (event.key === decrease) apply(value <= min ? 0 : Math.max(min, value - step));
    else apply(value === 0 ? min : value + step);
    save();
  });
  apply(value);
  const observer = new ResizeObserver(() => {
    if (splitter.getClientRects().length && !drag) apply(value);
  });
  observer.observe(splitter.parentElement);
  return { toggle: () => { apply(value ? 0 : defaultSize); save(); }, expand: () => { apply(value || defaultSize); save(); } };
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function chineseName(item) {
  const label = String(item.chn_name ?? "").trim();
  return label && !["null", "\\n"].includes(label.toLowerCase()) ? label : item.eng_name || "";
}
function action(text, handler, className = "link") {
  const button = node("button", text, className); button.type = "button"; button.onclick = handler; return button;
}
function renderLogs() {
  const target = el("logs"); if (!target) return;
  target.replaceChildren();
  if (state.auditLoaded) {
    if (!state.auditRecords.length) { target.append(node("div", "没有符合条件的日志。", "empty")); return; }
    for (const record of state.auditRecords) {
      const line = node("div", formatAuditLog(record), `log-line ${record.status === "failure" ? "failure" : "success"}`);
      line.title = record.error?.message || record.operation || "";
      target.append(line);
    }
    return;
  }
  if (state.logs.length) for (const message of state.logs) target.append(node("div", message, "log-line"));
  else target.append(node("div", "没有符合条件的日志。", "empty"));
}
function log(message, error = false) {
  state.logs.unshift(`${new Date().toLocaleString()} [${error ? "失败" : "操作"}] ${message}`);
  state.logs.length = Math.min(state.logs.length, 500); renderLogs();
}
function formatAuditLog(record) {
  const time = record.timestamp ? new Date(record.timestamp).toLocaleString() : "未知时间";
  const status = record.status === "success" ? "成功" : "失败";
  const category = { structure: "结构", data: "数据", system: "系统" }[record.category] || record.category || "操作";
  const parts = [`${time} [${status}] [${category}] ${record.operation || "操作"}`];
  if (record.table) parts.push(`表=${record.table}`);
  if (record.http_status != null) parts.push(`HTTP=${record.http_status}`);
  if (record.duration_ms != null) parts.push(`耗时=${record.duration_ms} ms`);
  if (record.result && Object.keys(record.result).length) parts.push(`结果=${JSON.stringify(record.result, null, 0)}`);
  if (record.error) parts.push(`错误=${record.error.message || JSON.stringify(record.error)}`);
  return parts.join(" | ");
}
async function loadAuditLogs() {
  const query = state.logQuery;
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) });
  for (const [key, value] of Object.entries({ date: query.date, keyword: query.keyword, category: query.category, status: query.status })) {
    if (value) params.set(key, value);
  }
  const response = await fetch(`/api/manage/logs?${params}`, { headers: { "Content-Type": "application/json" } });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `加载日志失败 ${response.status}`);
  state.auditRecords = Array.isArray(payload.logs) ? payload.logs : [];
  state.auditLoaded = true;
  state.logDates = Array.isArray(payload.dates) ? payload.dates : [];
  state.logQuery = { ...state.logQuery, page: payload.page || query.page, pageSize: payload.page_size || query.pageSize,
    total: payload.total || 0, pages: payload.pages || 1 };
  if (payload.config) state.logConfig = payload.config;
  renderLogDates(); renderLogSummary();
  renderLogs();
}
function renderLogDates() {
  const select = el("log-date"); if (!select) return;
  const selected = state.logQuery.date;
  select.replaceChildren(new Option("全部日期", ""));
  for (const value of state.logDates) select.append(new Option(value, value));
  if (selected && !state.logDates.includes(selected)) select.append(new Option(selected, selected));
  select.value = selected;
}
function renderLogSummary() {
  const query = state.logQuery;
  el("log-summary").textContent = `第 ${query.page} / ${query.pages} 页，共 ${query.total} 条`;
  el("log-page-label").textContent = `第 ${query.page} / ${query.pages} 页`;
  el("log-prev").disabled = query.page <= 1;
  el("log-next").disabled = query.page >= query.pages;
  if (state.logConfig) el("log-retention-days").value = state.logConfig.retention_days ?? 0;
}
async function loadLogConfig() {
  const config = await request("log-config");
  state.logConfig = config;
  if (Array.isArray(config.dates)) state.logDates = config.dates;
  renderLogDates(); renderLogSummary();
}
function resetLogPage() { state.logQuery.page = 1; }
function notice() { /* Compact layout: operation feedback is kept in the persistent log tab. */ }
async function run(work, errorTarget = null) {
  if (state.busy) return;
  state.busy = true; document.body.dataset.busy = "true";
  if (errorTarget) el(errorTarget).textContent = "";
  try { await work(); }
  catch (error) { log(error.message, true); if (errorTarget) el(errorTarget).textContent = error.message; else notice(error.message, true); }
  finally {
    if (el("tab-logs")?.classList.contains("active")) {
      try { await loadAuditLogs(); } catch { /* Keep the last visible log when the log endpoint is unavailable. */ }
    }
    state.busy = false; document.body.dataset.busy = "false";
  }
}
async function request(path, method = "GET", body) {
  const response = await fetch(`/api/manage/${path}`, { method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `请求失败 ${response.status}`);
  return payload;
}
const tablePath = (actionName, name = state.table) => `tables/${encodeURIComponent(name)}/${actionName}`;
function requireTable() { if (!state.table) throw new Error("请先选择或保存一张表"); }
function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}
function databaseFileDefinitionText(file) {
  const embedded = file.embedded_definition_exists ? "SQLite 内置 ✓" : "SQLite 内置 ×";
  return `${embedded} / ${file.definition_exists ? "JSON ✓" : "JSON ×"} / ${file.python_exists ? "Python ✓" : "Python ×"}`;
}
function renderDatabaseFiles() {
  const target = el("database-file-list");
  target.replaceChildren();
  if (!state.databaseFiles.length) {
    target.append(node("p", "服务端数据目录中没有 .sqlite3 文件。可在下方新建。", "empty"));
    return;
  }
  const table = node("table"), head = node("tr");
  ["文件", "表名", "记录数", "定义文件", "状态", "操作"].forEach((label) => head.append(node("th", label)));
  table.append(node("thead")); table.tHead.append(head);
  const body = node("tbody");
  for (const file of state.databaseFiles) {
    const row = node("tr");
    const fileTables = Array.isArray(file.tables) ? file.tables : [];
    const names = fileTables.map((entry) => entry.name);
    const name = state.tables.find((item) => names.includes(item.name)) || state.tables.find((item) => item.name === file.name);
    const fileCell = node("td", `${file.filename} · ${formatBytes(file.size_bytes)}`); fileCell.title = file.path || file.filename;
    const labels = names.length ? names.map((tableName) => {
      const item = state.tables.find((candidate) => candidate.name === tableName);
      return item ? `${chineseName(item.definition.head)} / ${tableName}` : tableName;
    }) : [name ? `${chineseName(name.definition.head)} / ${file.name}` : file.name];
    const tableCell = node("td", labels.join("、")); tableCell.title = labels.join("\n");
    const countCell = node("td", file.row_count == null ? "—" : Number(file.row_count).toLocaleString());
    const definitionCell = node("td", databaseFileDefinitionText(file));
    const statusCell = node("td", file.status || "未知", `file-status ${file.selectable ? "ok" : "bad"}`); statusCell.title = file.error || file.status || "";
    const actionCell = node("td");
    if (file.selectable) actionCell.append(action("打开", () => run(() => selectDatabaseFile(file.filename)), "secondary small"));
    else actionCell.append(action("处理报警", () => openDatabaseRepair(file), "secondary small"));
    row.append(fileCell, tableCell, countCell, definitionCell, statusCell, actionCell); body.append(row);
  }
  table.append(body); target.append(table);
}
function openDatabaseRepair(file) {
  state.databaseRepairFile = file;
  el("database-repair-file").textContent = `${file.filename} · ${file.status || "未知状态"}`;
  el("database-repair-status").textContent = file.error || "请选择一种处理方式。";
  el("database-repair-error").textContent = "";
  el("database-repair-dialog").showModal();
}
async function repairDatabase(actionName) {
  const file = state.databaseRepairFile;
  if (!file) throw new Error("未选择待处理的数据库文件");
  if (actionName === "delete-orphan" && !await confirmAction("删除未定义数据表", `将删除 ${file.filename}。文件会先归档，确认后继续。`, file.filename)) return;
  const result = await request(`database-files/${actionName}`, "POST", {
    filename: file.filename, ...(actionName === "delete-orphan" ? { confirm: file.filename } : {})
  });
  el("database-repair-dialog").close(); state.databaseRepairFile = null;
  await refreshTables(); await loadDatabaseFiles();
  const message = actionName === "delete-orphan" ? `已删除未定义数据表：${file.filename}（已归档）`
    : actionName === "rebuild-embedded-definition" ? `已重新生成内置定义表：${file.filename}` : `已重建数据表：${file.filename}`;
  notice(result.warning ? `${message}；${result.warning}` : message); log(message);
}
async function loadDatabaseFiles() {
  const result = await request("database-files");
  state.databaseFiles = Array.isArray(result.files) ? result.files : [];
  el("database-file-directory").textContent = `服务端数据目录：${result.data_dir || "—"}`;
  el("database-file-directory").title = `定义目录：${result.schema_dir || "—"}`;
  renderDatabaseFiles();
}
async function selectDatabaseFile(filename) {
  if (!await discardSchema() || !await discardCellEdits()) return;
  const result = await request("database-files/select", "POST", { filename });
  el("database-file-dialog").close();
  await refreshTables();
  const names = Array.isArray(result.file?.tables) ? result.file.tables.map((item) => item.name) : [];
  const target = names.includes(state.table) ? state.table : names[0] || result.name;
  if (target && state.tables.some((item) => item.name === target)) await selectTable(target);
  notice(`已选择服务端数据库文件：${filename}${names.length > 1 ? `，包含 ${names.length} 张表` : ""}`);
}
function setDatabaseFileTab(name) {
  document.querySelectorAll("[data-database-file-tab]").forEach((button) => {
    const active = button.dataset.databaseFileTab === name;
    button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active));
    const panel = el(button.getAttribute("aria-controls")); panel.hidden = !active;
  });
  el("database-file-error").textContent = "";
}
function fileToBase64(file) {
  return file.arrayBuffer().then((buffer) => {
    const bytes = new Uint8Array(buffer); let binary = ""; const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, Math.min(offset + chunkSize, bytes.length)));
    }
    return btoa(binary);
  });
}
function renderDatabaseUploadSelection() {
  const files = [...(el("database-file-upload-input").files || [])];
  const target = el("database-file-upload-selection");
  target.classList.toggle("has-files", Boolean(files.length));
  target.textContent = files.length ? files.map((file) => `${file.name} · ${formatBytes(file.size)}`).join("\n") : "尚未选择文件";
  el("upload-database-file").disabled = !files.length;
}
async function uploadDatabaseFile() {
  if (!await discardSchema() || !await discardCellEdits()) return;
  const files = [...(el("database-file-upload-input").files || [])];
  if (!files.length) throw new Error("请选择数据库文件");
  const total = files.reduce((sum, file) => sum + file.size, 0);
  if (files.some((file) => file.size > 16 * 1024 * 1024) || total > 22 * 1024 * 1024) throw new Error("单个文件不超过 16 MiB，合计不超过 22 MiB");
  const database = files.find((file) => /\.sqlite3$/i.test(file.name));
  if (!database) throw new Error("必须选择 .sqlite3 数据库文件");
  const name = database.name.replace(/\.sqlite3$/i, "");
  const existing = state.databaseFiles.some((item) => item.filename === database.name);
  if (existing && !await confirmAction("覆盖服务端数据库文件", `将覆盖服务端 ${database.name}，同时更新同名定义文件。`, name)) return;
  const encoded = [];
  for (const file of files) encoded.push({ name: file.name, content: await fileToBase64(file) });
  const result = await request("database-files/upload", "POST", { files: encoded, ...(existing ? { confirm: name } : {}) });
  el("database-file-dialog").close(); el("database-file-upload-input").value = ""; renderDatabaseUploadSelection();
  await refreshTables(); await loadDatabaseFiles();
  const target = result.tables?.[0]?.name || result.name;
  if (target && state.tables.some((item) => item.name === target)) await selectTable(target);
  const message = `${result.replaced ? "已覆盖并更新" : "已上传"} ${result.filename}，${result.tables?.length || 1} 张表，${Number(result.row_count).toLocaleString()} 条记录`;
  notice(message); log(message);
}
async function createDatabaseFile() {
  if (!await discardSchema() || !await discardCellEdits()) return;
  const name = el("database-file-new-name").value.trim(), label = el("database-file-new-label").value.trim();
  if (!name) throw new Error("英文表名不能为空");
  const result = await request("database-files/create", "POST", { name, chn_name: label || name });
  el("database-file-new-name").value = ""; el("database-file-new-label").value = "";
  await refreshTables(); await loadDatabaseFiles(); await selectTable(result.name); el("database-file-dialog").close();
  notice(`已新建服务端数据库文件：${result.filename}`); log(`新建数据库文件：${result.filename}`);
}
function download(text, filename, mime = "text/plain;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const anchor = node("a"); anchor.href = url; anchor.download = filename; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 10000);
}
function confirmAction(title, message, expected = "") {
  const dialog = el("confirm-dialog"); el("confirm-title").textContent = title; el("confirm-text").textContent = message;
  el("confirm-label").hidden = !expected; el("confirm-input").value = ""; el("confirm-input").placeholder = expected;
  el("confirm-action").disabled = Boolean(expected);
  el("confirm-input").oninput = () => { el("confirm-action").disabled = el("confirm-input").value !== expected; };
  dialog.returnValue = ""; dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirmed"), { once: true }));
}
function draftRecords() { return state.records.filter((record) => record.draftId); }
function hasPendingData() { return state.pendingEdits.size > 0 || state.records.some((record) => record.draftId); }
function recordKey(record) { return record?.draftId ? `draft:${record.draftId}` : JSON.stringify(record?.ref?.key ?? []); }
function isReadOnlyKey(record, column) { return column.primary_key && !record.draftId; }
function cellKey(record, field) { return `${recordKey(record)}\u0000${field}`; }
function columnKind(column) { return String(column?.data_type || "str").toLowerCase(); }
function displayColumnType(column) {
  const kind = columnKind(column);
  if (integerTypes.has(kind) || ["uchar", "short", "uint", "ulong", "datetime"].includes(kind)) return "INT";
  if (floatTypes.has(kind)) return "FLOAT";
  if (boolTypes.has(kind)) return "BOOL";
  if (objectTypes.has(kind)) return kind === "list" || kind === "array" ? "LIST" : kind === "dict" ? "DICT" : "JSON";
  if (["str", "string", "char", "binary", "text"].includes(kind)) return "STR";
  return kind.toUpperCase();
}
function cellText(value) { return value == null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value); }
function parseCellValue(column, raw) {
  const text = String(raw ?? "");
  const kind = columnKind(column);
  if (integerTypes.has(kind)) {
    if (!text.trim()) return null;
    if (!/^-?\d+$/.test(text.trim())) throw new Error(`${column.eng_name} 必须是整数`);
    const value = BigInt(text.trim());
    if (value < -(2n ** 63n) || value >= 2n ** 63n) throw new Error(`${column.eng_name} 超出 64 位整数范围`);
    return Number.isSafeInteger(Number(value)) ? Number(value) : value.toString();
  }
  if (floatTypes.has(kind)) { if (!text.trim()) return null; const value = Number(text.trim()); if (!Number.isFinite(value)) throw new Error(`${column.eng_name} 必须是有限数值`); return value; }
  if (boolTypes.has(kind)) { if (!text.trim()) return null; if (!["true", "false"].includes(text.trim().toLowerCase())) throw new Error(`${column.eng_name} 必须是 true 或 false`); return text.trim().toLowerCase() === "true"; }
  if (objectTypes.has(kind)) { if (!text.trim()) return null; return JSON.parse(text); }
  return text;
}
function valuesEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}
function rawMatchesBaseline(column, raw, baseline) {
  try { return valuesEqual(parseCellValue(column, raw), baseline); } catch { return false; }
}
function pendingCell(record, field) { return state.pendingEdits.get(cellKey(record, field)); }
function updateEditActions() {
  const count = state.pendingEdits.size, drafts = draftRecords().length;
  el("save-cell-edits").hidden = !count && !drafts;
  el("discard-cell-edits").hidden = !count && !drafts;
  el("save-cell-edits").textContent = drafts ? `保存修改（新增 ${drafts} 条）` : `保存改单元格（${count}）`;
  el("chart-edit-hint").textContent = [drafts ? `${drafts} 条新增记录待保存` : "", count ? `${count} 个单元格已修改` : ""].filter(Boolean).join(" · ") || "未选择修改";
}
function setPendingCell(record, column, raw) {
  if (isReadOnlyKey(record, column)) return;
  const field = column.eng_name, baseline = record.baseline?.[field];
  let value;
  try { value = parseCellValue(column, raw); } catch { value = String(raw ?? ""); }
  if (rawMatchesBaseline(column, raw, baseline)) {
    record.row[field] = baseline;
    state.pendingEdits.delete(cellKey(record, field));
  } else {
    record.row[field] = value;
    state.pendingEdits.set(cellKey(record, field), { record, column, raw: String(raw ?? "") });
  }
  updateEditActions();
}
function renderGridCellValue(record, column, cell) {
  const pending = pendingCell(record, column.eng_name), value = pending ? pending.raw : record.draftId ? cellText(record.row[column.eng_name]) : display(record.row[column.eng_name]);
  cell.replaceChildren(node("span", value, "cell-value"));
  cell.classList.toggle("cell-modified", Boolean(pending));
  cell.title = isReadOnlyKey(record, column) ? "KEY 主键列：可选择，不可修改，冻结在左侧" : record.draftId ? "待新增记录：双击填写，KEY 必填；保存后写入数据库" : pending ? "已修改，保存前仅保留在当前页面" : value;
}
function syncGridCell(record, field) {
  const index = state.records.indexOf(record), cell = [...el("data-grid").querySelectorAll("[data-cell-row][data-cell-field]")]
    .find((candidate) => candidate.dataset.cellRow === String(index) && candidate.dataset.cellField === field);
  const column = state.resultColumns.find((item) => item.eng_name === field);
  if (!cell || !column) return;
  const input = cell.querySelector(".cell-editor");
  if (!input) { renderGridCellValue(record, column, cell); return; }
  const pending = pendingCell(record, field), value = pending ? pending.raw : cellText(record.row[field]);
  if (input.type === "checkbox") input.checked = value === "true" || value === true;
  else input.value = value;
  input.closest("td")?.classList.toggle("cell-modified", Boolean(pending));
  input.closest("td")?.setAttribute("title", pending ? "已修改，保存前仅保留在当前页面" : value || "NULL");
}
function inlineCellInput(record, column, index, cell) {
  const kind = columnKind(column);
  if (isReadOnlyKey(record, column) || objectTypes.has(kind) && !record.draftId) return false;
  if (cell.querySelector(".cell-editor")) return true;
  const input = node("input"); input.className = "cell-editor"; input.dataset.rowIndex = String(index); input.dataset.field = column.eng_name;
  input.setAttribute("aria-label", `${record.draftId ? `新增${record.draftId}` : record.ref.key.join(" / ")} ${column.eng_name}`);
  const pending = pendingCell(record, column.eng_name), raw = pending?.raw ?? cellText(record.row[column.eng_name]);
  const before = structuredClone(record.row[column.eng_name]);
  if (boolTypes.has(kind)) { input.type = "checkbox"; input.checked = raw === "true" || raw === true; }
  else { input.type = "text"; input.value = raw; input.spellcheck = false; }
  const change = () => {
    const next = input.type === "checkbox" ? String(input.checked) : input.value;
    setPendingCell(record, column, next); input.setCustomValidity("");
    if (!rawMatchesBaseline(column, next, record.baseline?.[column.eng_name])) cell.classList.add("cell-modified"); else cell.classList.remove("cell-modified");
    drawChart();
  };
  input.oninput = change; input.onchange = change;
  input.onblur = () => renderGridCellValue(record, column, cell);
  input.onkeydown = (event) => {
    if (!["Enter", "Escape"].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    if (event.key === "Escape") {
      input.oninput = input.onchange = null;
      record.row[column.eng_name] = before;
      if (pending) state.pendingEdits.set(cellKey(record, column.eng_name), pending);
      else state.pendingEdits.delete(cellKey(record, column.eng_name));
      updateEditActions(); drawChart();
    }
    input.blur(); cell.focus({ preventScroll: true });
  };
  const placeholder = cell.querySelector(".cell-value");
  placeholder.classList.add("cell-editor-placeholder");
  cell.replaceChildren(placeholder, input); cell.classList.toggle("cell-modified", Boolean(pending));
  cell.title = pending ? "已修改，保存前仅保留在当前页面" : raw || "NULL";
  input.focus({ preventScroll: true }); if (input.type !== "checkbox") input.select();
  return true;
}
async function discardSchema() {
  return !state.schemaDirty || await confirmAction("离开结构编辑", "表结构有未保存的修改，确认放弃这些修改？");
}
function tab(name) {
  closeSqlHistory();
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === name; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${name}`));
  if (name === "data") requestAnimationFrame(drawChart);
}
function bottomTab(name) {
  document.querySelectorAll("[data-bottom-tab]").forEach((button) => {
    const active = button.dataset.bottomTab === name;
    button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1;
    const panel = el(button.getAttribute("aria-controls")); panel.classList.toggle("active", active); panel.hidden = !active;
    el(`${button.dataset.bottomTab}-actions`).hidden = !active;
  });
  if (name === "logs") run(async () => { await loadLogConfig(); await loadAuditLogs(); });
  if (name === "chart") requestAnimationFrame(drawChart);
}
function filteredTables() {
  const query = el("table-filter").value.trim().toLowerCase();
  return state.tables.filter((item) => (!el("nonempty").checked || item.row_count > 0) &&
    [item.name, chineseName(item.definition?.head || { eng_name: item.name, chn_name: "" })].some((name) => el("exact-match").checked ? !query || name.toLowerCase() === query : name.toLowerCase().includes(query)));
}
function renderTables() {
  const target = el("table-list"); target.replaceChildren(); const visible = filteredTables();
  el("table-count").textContent = `${visible.length} / ${state.tables.length} 表`;
  if (!visible.length) target.append(node("p", state.tables.length ? "没有匹配的表" : "暂无表，点击“新建”开始", "empty"));
  for (const item of visible) {
    const unavailable = item.orphan || item.selectable === false;
    const entry = node("div", null, `table-entry${item.name === state.table ? " active" : ""}${unavailable ? " orphan" : ""}`);
    const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = state.checkedTables.has(item.name); checkbox.disabled = unavailable;
    checkbox.setAttribute("aria-label", `选择表 ${item.name}`);
    checkbox.onchange = () => checkbox.checked ? state.checkedTables.add(item.name) : state.checkedTables.delete(item.name);
    const button = action("", () => run(async () => { if (await discardSchema() && await discardCellEdits()) await selectTable(item.name); }), "table-item");
    button.disabled = unavailable;
    const label = node("span", null, "table-name");
    label.append(node("span", chineseName(item.definition?.head || { eng_name: item.name, chn_name: "" }), "table-name-chn"), node("small", item.name, "table-name-eng"));
    button.append(label, node("span", item.error ? "异常" : item.row_count == null ? "—" : item.row_count.toLocaleString(), "count"));
    button.title = item.error || item.name; entry.append(checkbox, button); target.append(entry);
  }
}
async function refreshTables() {
  state.tables = (await request("tables")).tables;
  state.checkedTables = new Set([...state.checkedTables].filter((name) => state.tables.some((item) => item.name === name)));
  renderTables();
}
function renderCurrentDatabaseFile() {
  const target = el("current-database-file");
  const filename = state.table ? `${state.table}.sqlite3` : "";
  const directory = state.dataDir.replace(/[\\/]+$/, "");
  const separator = state.dataDir.includes("\\") ? "\\" : "/";
  target.textContent = filename || "未选择数据库";
  target.title = filename ? `当前数据库：${directory ? directory + separator : ""}${filename}` : "请选择或新建一张表";
  target.setAttribute("aria-label", filename ? `当前数据库文件：${filename}` : "未选择数据库");
}
async function selectTable(name) {
  const item = state.tables.find((entry) => entry.name === name);
  if (item?.orphan || item?.selectable === false) throw new Error(item.error || `表 ${name} 缺少可用的 SQLite 结构定义`);
  const result = await request(tablePath("schema", name));
  state.table = name; state.definition = result.definition; state.version = result.version; state.tableTotal = result.row_count; state.schemaDirty = false;
  renderCurrentDatabaseFile();
  loadSqlHistory();
  state.offset = 0; state.fields = new Set(state.definition.columns.map((column) => column.eng_name));
  state.selected.clear(); state.selectedColumns.clear(); state.selectedCells.clear(); state.rowAnchor = state.columnAnchor = state.cellAnchor = null;
  state.records = []; state.resultColumns = []; state.lastQuery = null; state.pendingEdits.clear(); updateEditActions();
  clearAddedChartSeries(); state.chartQueryKey = null;
  el("sql-editor").value = defaultSql();
  el("current-table").textContent = `${chineseName(state.definition.head)} / ${name}`;
  el("table-meta").textContent = `${state.definition.columns.length} 个字段 · 主键 ${state.definition.columns.filter((column) => column.primary_key).map((column) => column.eng_name).join(" + ")}`;
  renderTables(); renderFields(); renderSchema(state.definition); await queryRows();
}
async function refresh() {
  if (!await discardSchema()) return;
  if (!await discardCellEdits()) return;
  await refreshTables();
  if (state.table && state.tables.some((item) => item.name === state.table && !item.orphan && item.selectable !== false)) {
    const result = await request(tablePath("schema")); state.definition = result.definition; state.version = result.version; state.tableTotal = result.row_count;
    state.schemaDirty = false; renderSchema(state.definition); renderFields(); await queryRows();
  } else {
    const firstSelectable = state.tables.find((item) => !item.orphan && item.selectable !== false);
    if (firstSelectable) await selectTable(firstSelectable.name); else resetSelection();
  }
}
function resetSelection() {
  state.table = null; state.definition = null; state.version = null; state.records = []; state.total = 0; state.tableTotal = 0;
  renderCurrentDatabaseFile();
  state.history = []; closeSqlHistory();
  state.selected.clear(); state.selectedColumns.clear(); state.selectedCells.clear(); state.rowAnchor = state.columnAnchor = state.cellAnchor = null;
  state.fields.clear(); state.schemaDirty = false; state.offset = 0;
  state.resultColumns = []; state.editable = false; state.lastQuery = null; state.pendingEdits.clear(); updateEditActions(); el("sql-editor").value = "";
  clearAddedChartSeries(); state.chartQueryKey = null;
  el("current-table").textContent = "请选择或新建一张表"; el("table-meta").textContent = "—";
  el("schema-fields").replaceChildren(); el("schema-name").value = ""; el("schema-label").value = "";
  renderGrid(); renderFields(); configureChart(); renderTables();
}
function renderFields() {
  keepKeyFieldsVisible();
  const target = el("field-list"); target.replaceChildren();
  for (const column of state.resultColumns) {
    const label = node("label", null, "field-item"); const checkbox = node("input"); checkbox.type = "checkbox";
    checkbox.checked = state.fields.has(column.eng_name); checkbox.setAttribute("aria-label", `显示 ${column.eng_name}`);
    checkbox.disabled = Boolean(column.primary_key);
    if (column.primary_key) label.title = "KEY 主键列：始终显示并冻结在左侧，可选择，不可修改";
    checkbox.onchange = () => { if (checkbox.checked) state.fields.add(column.eng_name); else state.fields.delete(column.eng_name); renderGrid(); updateFieldCount(); };
    const type = node("small", displayColumnType(column), "field-type");
    type.title = `数据类型：${column.data_type || "str"}`;
    label.append(checkbox, node("span", chineseName(column)), node("small", column.eng_name, "field-name-eng"), type, node("b", column.primary_key ? "PK" : "", "key-tag"));
    target.append(label);
  }
  updateFieldCount();
}
function updateFieldCount() { el("field-count").textContent = `${state.fields.size} / ${state.resultColumns.length}`; }
const defaultSql = () => state.table ? `SELECT * FROM "${state.table}"` : "";
async function queryRows({ reset = false, silent = false, sql = state.lastQuery?.sql || defaultSql(), order = state.lastQuery?.order || null } = {}) {
  if (!state.table) return;
  if (hasPendingData()) { if (silent) return; throw new Error("请先保存或撤销待新增记录和单元格修改"); }
  if (reset) state.offset = 0;
  const result = await request(tablePath("sql"), "POST", { sql, order, readonly_only: true, limit: Number(el("page-size").value), offset: state.offset });
  if (state.offset >= result.total && state.offset > 0) {
    state.offset = Math.max(0, Math.ceil(result.total / result.limit) - 1) * result.limit;
    return queryRows({ silent, sql, order });
  }
  const previousColumns = state.resultColumns;
  const chartQueryKey = JSON.stringify([state.table, sql, order, state.offset, result.limit]);
  if (state.chartQueryKey !== chartQueryKey || !result.editable) clearAddedChartSeries();
  state.chartQueryKey = chartQueryKey;
  const changedColumns = JSON.stringify(previousColumns.map((column) => column.eng_name)) !== JSON.stringify(result.columns);
  state.resultColumns = result.columns.map((name) => {
    const defined = state.definition.columns.find((column) => column.eng_name === name);
    return defined || { eng_name: name, chn_name: name, data_type: result.rows.some((row) => typeof row[name] === "number") ? "float" : "str", primary_key: false };
  });
  if (changedColumns) state.fields = new Set(result.columns);
  state.total = result.total; state.tableTotal = result.table_total; state.editable = result.editable; updateSchemaLocks();
  state.lastQuery = { sql, order }; state.records = result.editable ? result.records : result.rows.map((row) => ({ row }));
  state.records.forEach((record) => { record.baseline = structuredClone(record.row); });
  rebindAddedChartSeries();
  state.pendingEdits.clear(); updateEditActions();
  state.selected.clear(); state.selectedColumns.clear(); state.selectedCells.clear(); state.rowAnchor = state.columnAnchor = state.cellAnchor = null;
  resetChartCursor();
  renderFields(); renderGrid();
  if (JSON.stringify(previousColumns) !== JSON.stringify(state.resultColumns)) configureChart();
  el("row-range").title = `${result.elapsed_ms.toFixed(1)} ms · ${sql}`;
  el("last-refresh").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
  const item = state.tables.find((entry) => entry.name === state.table); if (item) item.row_count = result.table_total; renderTables();
  if (!silent) log(`查询 ${state.table}：${result.total} 条，当前页 ${state.records.length} 条；${sql}`);
  drawChart();
}
function display(value) { return value == null ? "NULL" : typeof value === "object" ? JSON.stringify(value) : String(value); }
function closeRowContextMenu(restoreFocus = false) {
  const menu = el("row-context-menu"), trigger = state.contextRecord?.trigger;
  menu.hidden = true; state.contextRecord = null;
  if (restoreFocus) (trigger?.isConnected ? trigger : el("data-grid")).focus({ preventScroll: true });
}
function openRowContextMenu(event, record, index) {
  event.preventDefault(); event.stopPropagation();
  if (state.busy) return;
  const cell = event.target.closest("td[data-cell-field]");
  if (cell && !state.selected.has(index) && !state.selectedColumns.has(cell.dataset.cellField) && !state.selectedCells.has(cellSelectionKey(index, cell.dataset.cellField))) {
    selectGridCells(index, Number(cell.dataset.cellColumn), index, Number(cell.dataset.cellColumn));
  } else if (!cell && record && !state.selected.size && !state.selectedColumns.size && !state.selectedCells.size) selectGridRow(index, {});
  const selection = chartSelection();
  const menu = el("row-context-menu"); state.contextRecord = { record, index, selection, trigger: event.target.closest("[tabindex]") || el("data-grid") };
  const hasTable = Boolean(state.table), hasRows = Boolean(state.editable && state.selected.size), hasQuery = Boolean(state.lastQuery);
  const pending = hasPendingData();
  const setDisabled = (actionName, disabled) => { menu.querySelector(`[data-action="${actionName}"]`).disabled = disabled; };
  setDisabled("add-row", !hasTable || draftRecords().length >= 500);
  const rowIndexes = selectedRowIndexes(record, index);
  setDisabled("copy-rows", !state.editable || !rowIndexes.length);
  setDisabled("insert-copied-rows", !state.editable || !canInsertCopiedRows() || draftRecords().length + (state.copiedRows?.rows.length || 0) > 500);
  setDisabled("import", !hasTable || pending);
  setDisabled("export-json", !hasTable || !hasQuery);
  setDisabled("export-csv", !hasTable || !hasQuery || !state.fields.size);
  const hasCellSelection = Boolean(gridSelectionModel());
  setDisabled("copy-cells", !hasCellSelection);
  setDisabled("paste-cells", !hasTable || !state.editable || !hasCellSelection);
  setDisabled("batch-edit", !hasRows || pending);
  setDisabled("delete-selected", !state.editable || !rowIndexes.length);
  setDisabled("clear-table", !hasTable || pending);
  menu.querySelector('[data-action="batch-edit"]').title = pending ? "请先保存或撤销单元格修改" : hasRows ? `操作当前页选中的 ${state.selected.size} 行` : "点击行标题选择记录后操作";
  menu.querySelector('[data-action="delete-selected"]').title = !state.editable ? "当前查询结果不可删除，请切换到完整表查询" : rowIndexes.length
    ? `删除选区涉及的 ${rowIndexes.length} 条整行记录；保留其他行的待保存修改` : "请选择行或单元格区域";
  const canPlot = selection.fields.some((column) => selection.indexes.some((rowIndex) =>
    (!selection.cells.size || selection.cells.has(cellSelectionKey(rowIndex, column.eng_name))) && chartNumber(state.records[rowIndex].row[column.eng_name]) !== null));
  for (const actionName of ["chart-left", "chart-right"]) {
    const button = menu.querySelector(`[data-action="${actionName}"]`); button.disabled = !canPlot;
    button.title = canPlot ? `${selection.indexes.length} 行 × ${selection.columnCount} 列，${selection.mode === "rows" ? "按行绘制，KEY 为曲线名" : "按列绘制，列名为曲线名"}` : "选区中没有可绘制的数值";
  }
  menu.hidden = false;
  menu.scrollTop = 0;
  const margin = 8, width = menu.offsetWidth, height = menu.offsetHeight;
  menu.style.left = `${Math.max(margin, Math.min(event.clientX, window.innerWidth - width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(event.clientY, window.innerHeight - height - margin))}px`;
  (menu.querySelector("button:not(:disabled)") || menu).focus({ preventScroll: true });
}
async function deleteRecords(records, title) {
  requireTable();
  if (!state.editable) throw new Error("当前查询结果不可删除，请切换到完整表查询");
  if (!records.length || records.some((record) => !record || !state.records.includes(record))) throw new Error("请选择有效记录");
  const saved = records.filter((record) => !record.draftId), drafts = records.length - saved.length;
  const details = [saved.length ? `${saved.length} 条数据库记录` : "", drafts ? `${drafts} 条待保存新行` : ""].filter(Boolean).join("和");
  if (!await confirmAction(title, `删除 ${state.table} 中选中的 ${records.length} 条整行记录（${details}）？选中行的待保存修改会一并移除，其他行的待保存内容保留。`)) return;
  // Keep all local drafts and edits intact until the server has deleted the saved rows atomically.
  if (saved.length) await request(tablePath("rows"), "DELETE", { records: saved.map((record) => record.ref) });
  const removed = new Set(records.map(recordKey));
  state.records = state.records.filter((record) => !removed.has(recordKey(record)));
  for (const [key, edit] of state.pendingEdits) if (removed.has(recordKey(edit.record))) state.pendingEdits.delete(key);
  state.total = Math.max(0, state.total - saved.length); state.tableTotal = Math.max(0, state.tableTotal - saved.length);
  const table = state.tables.find((item) => item.name === state.table); if (table) table.row_count = state.tableTotal;
  state.selected.clear(); state.selectedColumns.clear(); state.selectedCells.clear();
  state.rowAnchor = state.columnAnchor = state.cellAnchor = null;
  state.chartSeries = state.chartSeries.filter((series) => {
    series.points = series.points.filter((point) => !removed.has(point.identity));
    if (series.points.length) return true;
    state.chartAxes.delete(series.key); state.chartHidden.delete(series.key); return false;
  });
  state.chartCleared = !state.chartSeries.length;
  rebindAddedChartSeries(); resetChartCursor(); renderTables(); renderGrid(); drawChart();
  const message = [saved.length ? `已删除 ${saved.length} 条数据库记录` : "", drafts ? `已移除 ${drafts} 条待保存新行` : ""].filter(Boolean).join("，");
  log(`${state.table}：${message}`); notice(message);
  if (!hasPendingData()) {
    try { await queryRows(); }
    catch (error) { notice(`${message}；刷新失败：${error.message}`, true); log(`刷新 ${state.table} 失败：${error.message}`, true); }
  }
}
function openImportDialog() {
  requireTable();
  if (hasPendingData()) throw new Error("请先保存或撤销待新增记录和单元格修改，再导入数据");
  el("import-error").textContent = ""; el("import-dialog").showModal();
}
async function exportQuery(format) {
  requireTable();
  if (format === "csv" && !state.fields.size) throw new Error("请至少选择一个导出字段");
  if (!state.lastQuery) throw new Error("请先执行查询");
  const result = await request(tablePath("sql-export"), "POST", { ...state.lastQuery, format, columns: [...state.fields] });
  download(result.text, result.filename, result.mime); log(`导出 ${state.table} 全部匹配记录（${format}）`);
}
async function deleteSelectedRows(record, index) {
  await deleteRecords(selectedRowIndexes(record, index).map((rowIndex) => state.records[rowIndex]), "删除选中记录");
}
async function clearTable() {
  requireTable();
  if (hasPendingData()) throw new Error("请先保存或撤销待新增记录和单元格修改，再清空表");
  const snapshot = await request(tablePath("snapshot"));
  if (!await confirmAction("清空表", `删除 ${state.table} 全部 ${snapshot.count} 条记录（不受当前筛选条件限制）。`, state.table)) return;
  await request(tablePath("clear"), "POST", { confirm: state.table, version: snapshot.version }); log(`清空 ${state.table}`); notice("表已清空"); await queryRows({ reset: true });
}
function handleRowContextAction(actionName) {
  const context = state.contextRecord; closeRowContextMenu(); if (!context) return;
  const { record, index } = context;
  if (actionName === "add-row") run(() => openAddRows(record, index));
  else if (actionName === "copy-rows") run(() => copySelectedRows(record, index));
  else if (actionName === "insert-copied-rows") run(() => insertCopiedRows(record, index));
  else if (actionName === "import") run(openImportDialog);
  else if (actionName === "export-json") run(() => exportQuery("json"));
  else if (actionName === "export-csv") run(() => exportQuery("csv"));
  else if (actionName === "copy-cells") run(copySelectedCells);
  else if (actionName === "paste-cells") run(pasteFromClipboard);
  else if (actionName === "batch-edit") run(() => openRow("batch"));
  else if (actionName === "delete-selected") run(() => deleteSelectedRows(record, index));
  else if (actionName === "clear-table") run(clearTable);
  else if (actionName === "chart-left" || actionName === "chart-right") run(() => addSelectionCurves(context.selection, actionName === "chart-left" ? "left" : "right"));
}
function cellSelectionKey(index, field) { return `${index}\u0000${field}`; }
function splitCellSelectionKey(key) { const separator = key.indexOf("\u0000"); return { index: Number(key.slice(0, separator)), field: key.slice(separator + 1) }; }
function keepKeyFieldsVisible() {
  state.resultColumns.filter((column) => column.primary_key).forEach((column) => state.fields.add(column.eng_name));
}
function visibleGridColumns() {
  const keys = state.resultColumns.filter((column) => column.primary_key);
  const fields = state.resultColumns.filter((column) => !column.primary_key && state.fields.has(column.eng_name));
  return [...keys, ...fields];
}
function markFrozenKey(cell, index) {
  cell.classList.add("key-column");
  cell.dataset.frozenIndex = String(index);
  cell.style.left = `var(--key-left-${index}, 36px)`;
  cell.title = "KEY 主键列：可选择，不可修改，冻结在左侧";
}
function updateFrozenKeyOffsets() {
  const grid = el("data-grid").querySelector("table");
  const corner = grid?.querySelector(".row-corner");
  if (!corner || !corner.getBoundingClientRect().width) return;
  let offset = corner.getBoundingClientRect().width;
  grid.querySelectorAll("thead .key-column").forEach((header) => {
    grid.style.setProperty(`--key-left-${header.dataset.frozenIndex}`, `${offset}px`);
    offset += header.getBoundingClientRect().width;
  });
}
const frozenKeyObserver = new ResizeObserver(updateFrozenKeyOffsets);
function clearGridSelection(except = "") {
  if (except !== "rows") state.selected.clear();
  if (except !== "columns") state.selectedColumns.clear();
  if (except !== "cells") state.selectedCells.clear();
  if (except !== "rows") state.rowAnchor = null;
  if (except !== "columns") state.columnAnchor = null;
  if (except !== "cells") state.cellAnchor = null;
}
function rangeIndexes(start, end) {
  const first = Math.min(start, end), last = Math.max(start, end); return Array.from({ length: last - first + 1 }, (_item, offset) => first + offset);
}
function selectGridRow(index, event) {
  const additive = event.ctrlKey || event.metaKey;
  if (event.shiftKey && state.rowAnchor !== null) {
    if (!additive) state.selected.clear(); rangeIndexes(state.rowAnchor, index).forEach((rowIndex) => state.selected.add(rowIndex));
  } else if (additive) {
    state.selected.has(index) ? state.selected.delete(index) : state.selected.add(index); state.rowAnchor = index;
  } else { state.selected = new Set([index]); state.rowAnchor = index; }
  clearGridSelection("rows");
  paintGridSelection();
}
function selectGridColumn(columnIndex, event) {
  const columns = visibleGridColumns(), field = columns[columnIndex]?.eng_name; if (!field) return;
  const additive = event.ctrlKey || event.metaKey;
  if (event.shiftKey && state.columnAnchor !== null) {
    if (!additive) state.selectedColumns.clear(); rangeIndexes(state.columnAnchor, columnIndex).forEach((index) => state.selectedColumns.add(columns[index].eng_name));
  } else if (additive) {
    state.selectedColumns.has(field) ? state.selectedColumns.delete(field) : state.selectedColumns.add(field); state.columnAnchor = columnIndex;
  } else { state.selectedColumns = new Set([field]); state.columnAnchor = columnIndex; }
  clearGridSelection("columns"); paintGridSelection();
}
function selectGridCells(startRow, startColumn, endRow, endColumn, additive = false) {
  const columns = visibleGridColumns(), firstRow = Math.min(startRow, endRow), lastRow = Math.max(startRow, endRow), firstColumn = Math.min(startColumn, endColumn), lastColumn = Math.max(startColumn, endColumn);
  if (!additive) state.selectedCells.clear();
  for (let rowIndex = firstRow; rowIndex <= lastRow; rowIndex++) for (let columnIndex = firstColumn; columnIndex <= lastColumn; columnIndex++) {
    const column = columns[columnIndex]; if (column) state.selectedCells.add(cellSelectionKey(rowIndex, column.eng_name));
  }
  state.cellAnchor = { row: startRow, column: startColumn }; clearGridSelection("cells"); paintGridSelection();
}
function startCellSelection(event, rowIndex, columnIndex) {
  if (event.button !== 0 || event.target.closest(".cell-editor")) return;
  event.currentTarget.focus({ preventScroll: true });
  const additive = event.ctrlKey || event.metaKey;
  const anchor = event.shiftKey && state.cellAnchor ? { ...state.cellAnchor } : { row: rowIndex, column: columnIndex };
  const base = additive ? new Set(state.selectedCells) : new Set();
  selectGridCells(anchor.row, anchor.column, rowIndex, columnIndex, additive);
  state.cellDrag = { pointerId: event.pointerId, ...anchor, base, moved: false };
  document.documentElement.classList.add("selecting-cells"); event.preventDefault();
}
function updateCellSelectionFromPointer(event) {
  const drag = state.cellDrag; if (!drag || drag.pointerId !== event.pointerId) return;
  const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-cell-row][data-cell-column]");
  if (!target) return;
  const rowIndex = Number(target.dataset.cellRow), columnIndex = Number(target.dataset.cellColumn);
  if (rowIndex !== drag.row || columnIndex !== drag.column) drag.moved = true;
  state.selectedCells = new Set(drag.base);
  selectGridCells(drag.row, drag.column, rowIndex, columnIndex, true);
}
function finishCellSelection(event) {
  const drag = state.cellDrag; if (!drag) return;
  state.cellDrag = null; document.documentElement.classList.remove("selecting-cells");
}
function gridSelectionModel() {
  const columns = visibleGridColumns();
  if (!columns.length || !state.records.length) return null;
  let rows = [], columnIndexes = [];
  if (state.selectedCells.size) {
    const cells = [...state.selectedCells].map(splitCellSelectionKey).filter(({ index, field }) => state.records[index] && columns.some((column) => column.eng_name === field));
    if (!cells.length) return null;
    const rowIndexes = cells.map(({ index }) => index), indexes = cells.map(({ field }) => columns.findIndex((column) => column.eng_name === field)).filter((index) => index >= 0);
    rows = rangeIndexes(Math.min(...rowIndexes), Math.max(...rowIndexes));
    columnIndexes = rangeIndexes(Math.min(...indexes), Math.max(...indexes));
  } else if (state.selected.size && state.selectedColumns.size) {
    rows = [...state.selected].filter((index) => state.records[index]).sort((left, right) => left - right);
    columnIndexes = columns.map((_column, index) => index).filter((index) => state.selectedColumns.has(columns[index].eng_name));
  } else if (state.selectedColumns.size) {
    rows = state.records.map((_record, index) => index);
    columnIndexes = columns.map((_column, index) => index).filter((index) => state.selectedColumns.has(columns[index].eng_name));
  } else if (state.selected.size) {
    rows = [...state.selected].filter((index) => state.records[index]).sort((left, right) => left - right);
    columnIndexes = columns.map((_column, index) => index);
  } else return null;
  if (!rows.length || !columnIndexes.length) return null;
  return { rows, columnIndexes, columns: columnIndexes.map((index) => columns[index]) };
}
function clipboardCellValue(record, column) {
  return pendingCell(record, column.eng_name)?.raw ?? cellText(record.row[column.eng_name]);
}
function gridSelectionClipboardText(model) {
  return model.rows.map((rowIndex) => model.columns.map((column) => clipboardCellValue(state.records[rowIndex], column)).join("\t")).join("\r\n");
}
function writeClipboardText(text) {
  const textarea = node("textarea"); textarea.value = text; textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed"; textarea.style.top = "0"; textarea.style.opacity = "0"; textarea.style.pointerEvents = "none";
  document.body.append(textarea); textarea.focus({ preventScroll: true }); textarea.select();
  let copied = false;
  const onCopy = (event) => {
    if (event.clipboardData) event.clipboardData.setData("text/plain", text);
    event.preventDefault(); copied = true;
  };
  document.addEventListener("copy", onCopy);
  try { copied = document.execCommand("copy") || copied; } catch { /* Use the asynchronous API below. */ }
  document.removeEventListener("copy", onCopy); textarea.remove();
  if (copied) return Promise.resolve();
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  return Promise.reject(new Error("浏览器拒绝访问剪贴板，请使用 Ctrl+C 或授予页面剪贴板权限"));
}
async function copySelectedCells() {
  requireTable();
  const model = gridSelectionModel(); if (!model) throw new Error("请先选择要复制的表格区域");
  await writeClipboardText(gridSelectionClipboardText(model));
  const size = `${model.rows.length} 行 × ${model.columns.length} 列`;
  notice(`已复制选中区域（${size}）`); log(`复制 ${state.table} 选中区域：${size}`);
}
function parseClipboardMatrix(text) {
  const normalized = String(text ?? "").replace(/\r\n?/g, "\n");
  if (!normalized) return [];
  const lines = normalized.endsWith("\n") ? normalized.slice(0, -1).split("\n") : normalized.split("\n");
  return lines.map((line) => line.split("\t"));
}
async function pasteClipboardText(text) {
  requireTable();
  if (!state.editable) throw new Error("当前查询结果只读，不能粘贴修改");
  const matrix = parseClipboardMatrix(text), model = gridSelectionModel();
  if (!matrix.length || !model) throw new Error("请先选择粘贴起点或目标区域");
  const columns = visibleGridColumns(), anchorRow = model.rows[0], anchorColumn = model.columnIndexes[0], changes = [];
  let skippedKeys = 0;
  for (let rowOffset = 0; rowOffset < matrix.length; rowOffset++) {
    const record = state.records[anchorRow + rowOffset];
    if (!record) throw new Error(`粘贴区域超出当前页（第 ${rowOffset + 1} 行没有对应记录）`);
    for (let columnOffset = 0; columnOffset < matrix[rowOffset].length; columnOffset++) {
      const column = columns[anchorColumn + columnOffset];
      if (!column) throw new Error(`粘贴区域超出当前显示字段（第 ${columnOffset + 1} 列没有对应字段）`);
      const raw = matrix[rowOffset][columnOffset];
      if (isReadOnlyKey(record, column)) { skippedKeys++; continue; }
      try { parseCellValue(column, raw); }
      catch (error) { throw new Error(`第 ${rowOffset + 1} 行“${column.eng_name}”字段值无效：${error.message}`); }
      changes.push({ record, column, raw });
    }
  }
  if (!changes.length) throw new Error("粘贴区域只包含 KEY 列，没有可修改的字段");
  changes.forEach(({ record, column, raw }) => setPendingCell(record, column, raw));
  changes.forEach(({ record, column }) => syncGridCell(record, column.eng_name));
  paintGridSelection(); drawChart(); updateEditActions();
  const message = `已粘贴 ${changes.length} 个单元格${skippedKeys ? `，跳过 ${skippedKeys} 个 KEY 单元格` : ""}，请保存改单元格`;
  notice(message); log(`${state.table}：${message}`);
}
async function pasteFromClipboard() {
  if (!navigator.clipboard?.readText) throw new Error("当前浏览器不支持读取剪贴板，请使用 Ctrl+V");
  let text;
  try { text = await navigator.clipboard.readText(); }
  catch { throw new Error("浏览器拒绝读取剪贴板，请使用 Ctrl+V 或授予页面剪贴板权限"); }
  await pasteClipboardText(text);
}
function syncChartFieldsToGridSelection() {
  const numeric = new Set(numericColumns().map((column) => column.eng_name));
  const fields = state.selectedColumns.size ? [...state.selectedColumns] : [...state.selectedCells].map((key) => splitCellSelectionKey(key).field);
  const selectedNumeric = [...new Set(fields)].filter((field) => numeric.has(field));
  if (selectedNumeric.length) state.chartFields = new Set(selectedNumeric);
  el("chart-fields").querySelectorAll("input").forEach((input) => { input.checked = state.chartFields.has(input.dataset.field); });
}
function paintGridSelection() {
  const boxModel = (state.selectedCells.size || state.selectedColumns.size) ? gridSelectionModel() : null;
  const boxRows = new Set(boxModel?.rows || []), boxColumns = new Set(boxModel?.columnIndexes || []);
  const firstRow = boxModel?.rows[0], lastRow = boxModel ? boxModel.rows[boxModel.rows.length - 1] : undefined, firstColumn = boxModel?.columnIndexes[0], lastColumn = boxModel ? boxModel.columnIndexes[boxModel.columnIndexes.length - 1] : undefined;
  document.querySelectorAll("#data-grid [data-grid-row]").forEach((row) => row.classList.toggle("selected", state.selected.has(Number(row.dataset.gridRow))));
  document.querySelectorAll("#data-grid [data-grid-column]").forEach((header) => header.classList.toggle("selected-header", state.selectedColumns.has(header.dataset.gridColumn)));
  document.querySelectorAll("#data-grid [data-cell-row][data-cell-field]").forEach((cell) => {
    const row = Number(cell.dataset.cellRow), column = Number(cell.dataset.cellColumn), selected = state.selectedColumns.has(cell.dataset.cellField) || state.selectedCells.has(cellSelectionKey(row, cell.dataset.cellField));
    cell.classList.toggle("cell-selected", selected);
    const boxed = Boolean(boxModel && boxRows.has(row) && boxColumns.has(column)), shadows = [];
    if (boxed && row === firstRow) shadows.push("inset 0 2px #4e8fec");
    if (boxed && row === lastRow) shadows.push("inset 0 -2px #4e8fec");
    if (boxed && column === firstColumn) shadows.push("inset 2px 0 #4e8fec");
    if (boxed && column === lastColumn) shadows.push("inset -2px 0 #4e8fec");
    cell.style.boxShadow = shadows.join(", ");
  });
}
function renderGrid() {
  finishCellSelection();
  keepKeyFieldsVisible();
  frozenKeyObserver.disconnect();
  const visible = new Set(visibleGridColumns().map((column) => column.eng_name));
  state.selectedColumns = new Set([...state.selectedColumns].filter((field) => visible.has(field)));
  state.selectedCells = new Set([...state.selectedCells].filter((key) => visible.has(splitCellSelectionKey(key).field)));
  state.columnAnchor = state.cellAnchor = null;
  closeRowContextMenu();
  const target = el("data-grid"); target.replaceChildren();
  if (!state.resultColumns.length) target.append(node("div", "从左侧选择表，或点击“新建”开始。", "empty"));
  else {
    const columns = visibleGridColumns();
    const grid = node("table"), head = node("tr"), thead = node("thead");
    {
      const corner = node("th", "↖", "row-corner"); corner.scope = "col"; corner.title = "选择当前页全部行和列"; corner.setAttribute("aria-label", "选择当前页全部单元格");
      corner.onclick = () => { state.selected = new Set(state.records.map((_record, index) => index)); state.selectedColumns = new Set(columns.map((column) => column.eng_name)); state.selectedCells.clear(); state.rowAnchor = state.records.length - 1; state.columnAnchor = columns.length - 1; state.cellAnchor = null; paintGridSelection(); };
      head.append(corner);
    }
    columns.forEach((column, columnIndex) => {
      const header = node("th", null, `column-header${state.selectedColumns.has(column.eng_name) ? " selected-header" : ""}`); header.scope = "col"; header.dataset.gridColumn = column.eng_name; header.tabIndex = 0;
      const sorted = state.lastQuery?.order?.column === column.eng_name, direction = state.lastQuery?.order?.direction;
      const sorting = sorted ? direction === "ASC" ? " ↑" : " ↓" : "";
      header.append(node("span", (el("chinese-columns").checked ? chineseName(column) : column.eng_name) + sorting));
      if (column.primary_key) {
        markFrozenKey(header, columnIndex);
        header.append(node("span", "KEY", "key-column-badge"));
      }
      header.onclick = (event) => selectGridColumn(columnIndex, event);
      header.oncontextmenu = (event) => {
        if (!state.selectedColumns.has(column.eng_name)) selectGridColumn(columnIndex, {});
        openRowContextMenu(event, null, null);
      };
      header.ondblclick = () => run(async () => {
        await queryRows({ reset: true, order: { column: column.eng_name, direction: sorted && direction === "ASC" ? "DESC" : "ASC" } });
      });
      if (el("chinese-columns").checked && chineseName(column) !== column.eng_name) header.append(node("small", column.eng_name)); head.append(header);
    });
    thead.append(head); grid.append(thead); const body = node("tbody");
    state.records.forEach((record, index) => {
      const row = node("tr", null, state.selected.has(index) ? "selected" : ""); row.dataset.gridRow = String(index);
      row.classList.toggle("draft-row", Boolean(record.draftId));
      row.addEventListener("contextmenu", (event) => openRowContextMenu(event, record, index));
      {
        const rowHeader = node("th", record.draftId ? `新${record.draftId}` : String(state.offset + index + 1), "row-header"); rowHeader.scope = "row"; rowHeader.dataset.gridRow = String(index); rowHeader.title = record.draftId ? "待新增记录，保存后写入数据库" : `选择第 ${state.offset + index + 1} 行：${record.ref?.key.join(" / ") || ""}`;
        rowHeader.onclick = (event) => selectGridRow(index, event); row.append(rowHeader);
      }
      columns.forEach((column, columnIndex) => {
        const cell = node("td", null, "value"); cell.dataset.cellRow = String(index); cell.dataset.cellColumn = String(columnIndex); cell.dataset.cellField = column.eng_name;
        cell.tabIndex = -1;
        if (column.primary_key) markFrozenKey(cell, columnIndex);
        cell.classList.toggle("cell-selected", state.selectedCells.has(cellSelectionKey(index, column.eng_name)));
        cell.addEventListener("pointerdown", (event) => { startCellSelection(event, index, columnIndex); });
        renderGridCellValue(record, column, cell);
        if (state.editable && !isReadOnlyKey(record, column)) cell.ondblclick = (event) => {
          if (event.target.closest(".cell-editor") || state.busy) return;
          event.preventDefault();
          if (!inlineCellInput(record, column, index, cell)) run(() => openRow("edit", record));
        };
        row.append(cell);
      });
      body.append(row);
    });
    if (!state.records.length) { const row = node("tr"), cell = node("td", "当前 SQL 查询没有记录。", "empty"); cell.colSpan = columns.length + 1; row.append(cell); body.append(row); }
    grid.append(body); target.append(grid);
    frozenKeyObserver.observe(grid);
    grid.querySelectorAll("thead .key-column, .row-corner").forEach((header) => frozenKeyObserver.observe(header));
    updateFrozenKeyOffsets();
  }
  paintGridSelection();
  updateEditActions();
  const pageSize = Number(el("page-size").value), pages = Math.max(1, Math.ceil(state.total / pageSize));
  el("page-number").value = String(Math.floor(state.offset / pageSize) + 1); el("page-number").max = String(pages); el("page-total").textContent = pages;
  el("prev").disabled = state.offset === 0; el("next").disabled = state.offset + pageSize >= state.total;
  const drafts = draftRecords().length;
  const savedCount = state.records.length - drafts;
  el("row-range").textContent = (savedCount ? `${state.offset + 1}–${state.offset + savedCount} / ${state.total.toLocaleString()} 条` : `当前页 0 / ${state.total.toLocaleString()} 条`) + (drafts ? ` · 新增 ${drafts} 条待保存` : "");
}
function openAddRows(record, index) {
  requireTable();
  const remaining = 500 - draftRecords().length;
  if (remaining <= 0) throw new Error("一次最多暂存 500 条新增记录，请先保存或撤销");
  state.addRowIndex = record ? index + 1 : null;
  el("add-row-count").value = "1"; el("add-row-count").max = String(remaining);
  el("add-rows-error").textContent = ""; el("add-rows-dialog").showModal();
  el("add-row-count").focus(); el("add-row-count").select();
}
function selectedRowIndexes(record, index) {
  const indexes = state.selected.size ? [...state.selected] : state.selectedCells.size
    ? [...new Set([...state.selectedCells].map((key) => splitCellSelectionKey(key).index))]
    : state.selectedColumns.size ? state.records.map((_record, rowIndex) => rowIndex) : record ? [index] : [];
  return indexes.filter((rowIndex) => state.records[rowIndex]).sort((left, right) => left - right);
}
function rowCopySchema() { return JSON.stringify(state.definition?.columns.map((column) => [column.eng_name, column.data_type, column.primary_key])); }
function canInsertCopiedRows() { return Boolean(state.copiedRows?.rows.length && state.copiedRows.schema === rowCopySchema()); }
function copySelectedRows(record, index) {
  requireTable();
  const indexes = selectedRowIndexes(record, index);
  if (!state.editable || !indexes.length) throw new Error("请先选择要复制的原始记录行");
  if (indexes.length > 500) throw new Error("一次最多复制 500 行");
  const rows = indexes.map((rowIndex) => {
    const source = state.records[rowIndex];
    return Object.fromEntries(state.definition.columns.map((column) => {
      const pending = pendingCell(source, column.eng_name), value = source.row[column.eng_name];
      return [column.eng_name, pending ? pending.raw : value == null ? null : cellText(value)];
    }));
  });
  state.copiedRows = { schema: rowCopySchema(), rows };
  notice(`已复制 ${rows.length} 行，可在目标位置右键选择“插入复制行”`);
  log(`${state.table}：复制选中 ${rows.length} 行`);
}
function insertCopiedRows(record, index) {
  requireTable();
  if (!state.editable || !canInsertCopiedRows()) throw new Error("请先复制字段结构相同的记录行");
  if (draftRecords().length + state.copiedRows.rows.length > 500) throw new Error("一次最多暂存 500 条新增记录，请先保存或撤销");
  const indexes = selectedRowIndexes(record, index);
  const copies = state.copiedRows.rows.map((source) => {
    const row = Object.fromEntries(state.definition.columns.map((column) => [column.eng_name, null]));
    const copy = { draftId: ++state.draftSequence, row, baseline: structuredClone(row) };
    for (const column of state.definition.columns) {
      const value = source[column.eng_name];
      if (value != null) setPendingCell(copy, column, value);
    }
    return copy;
  });
  const start = indexes.length ? indexes[indexes.length - 1] + 1 : state.records.length;
  state.records.splice(start, 0, ...copies);
  state.selected = new Set(copies.map((_copy, copyIndex) => start + copyIndex));
  state.selectedColumns.clear(); state.selectedCells.clear();
  rebindAddedChartSeries(); renderGrid(); drawChart();
  const cell = el("data-grid").querySelector(`td[data-cell-row="${start}"]`);
  cell?.scrollIntoView({ block: "nearest", inline: "nearest" }); cell?.focus({ preventScroll: true });
  notice(`已插入 ${copies.length} 条复制行，请修改重复的 KEY 后保存`);
  log(`${state.table}：暂存 ${copies.length} 条复制行，尚未写入数据库`);
}
async function addBlankRows() {
  requireTable();
  const count = Number(el("add-row-count").value), remaining = 500 - draftRecords().length;
  if (!Number.isInteger(count) || count < 1 || count > remaining) throw new Error(`请输入 1–${remaining} 的整数`);
  if (!state.editable) {
    await queryRows({ reset: true, sql: defaultSql(), order: null });
    el("sql-editor").value = defaultSql();
  }
  const first = state.addRowIndex === null ? state.records.length : Math.min(state.addRowIndex, state.records.length);
  const rows = Array.from({ length: count }, () => {
    const row = Object.fromEntries(state.definition.columns.map((column) => [column.eng_name, fieldDefault(column)]));
    return { draftId: ++state.draftSequence, row, baseline: structuredClone(row) };
  });
  state.records.splice(first, 0, ...rows);
  state.selected.clear(); state.selectedColumns.clear(); state.selectedCells.clear();
  state.rowAnchor = state.columnAnchor = state.cellAnchor = null;
  rebindAddedChartSeries(); renderGrid(); drawChart(); el("add-rows-dialog").close(); tab("data");
  const cell = el("data-grid").querySelector(`td[data-cell-row="${first}"]`);
  cell?.scrollIntoView({ block: "nearest", inline: "nearest" }); cell?.focus({ preventScroll: true });
  notice(`已加入 ${count} 条新记录，已填入字段默认值；请双击填写 KEY 和其他字段，再保存修改`);
  log(`${state.table}：按字段默认值暂存新增 ${count} 条记录，尚未写入数据库`);
}
function fieldDefault(column) {
  return structuredClone(column.default_value ?? null);
}
function openRow(mode, record = null) {
  requireTable();
  if (hasPendingData()) throw new Error("请先保存或撤销待新增记录和单元格修改");
  if (mode !== "add" && !state.editable) throw new Error("请使用 SQL 修改，或重置后选择原始记录");
  const rows = mode === "batch" ? [...state.selected].map((index) => state.records[index]) : record ? [record] : [];
  if (mode === "batch" && !rows.length) throw new Error("请先选择记录");
  state.dialog = { mode, table: state.table, records: structuredClone(rows), definition: structuredClone(state.definition) };
  el("row-dialog-title").textContent = ({ add: "新增记录", edit: "编辑记录", copy: "复制记录", batch: "批量修改" })[mode];
  el("row-hint").textContent = mode === "batch" ? `修改 ${rows.length} 条选中记录。JSON 只填写需要修改的非主键字段。` : mode === "copy" ? "请设置新的主键，原记录保持不变。" : "字段按定义类型校验；主键不可为空。";
  el("row-error").textContent = "";
  const values = mode === "batch" ? {} : record ? structuredClone(record.row) : Object.fromEntries(state.definition.columns.map((column) => [column.eng_name, fieldDefault(column)]));
  el("row-editor").value = JSON.stringify(values, null, 2); el("json-mode").checked = mode === "batch"; el("json-mode").disabled = mode === "batch";
  buildRowFields(values); el("row-fields").hidden = mode === "batch"; el("row-editor").hidden = mode !== "batch"; el("row-dialog").showModal();
}
function buildRowFields(values) {
  el("row-fields").replaceChildren();
  for (const column of state.dialog.definition.columns) {
    const kind = column.data_type.toLowerCase(), label = node("label", `${chineseName(column)} / ${column.eng_name}${column.primary_key ? " · 主键" : ""}`);
    const menu = column.disp_menu ? column.disp_menu.split("|").filter(Boolean) : [];
    const input = node(boolTypes.has(kind) || menu.length ? "select" : objectTypes.has(kind) ? "textarea" : "input");
    input.dataset.field = column.eng_name; input.setAttribute("aria-label", `字段 ${column.eng_name}`);
    if (boolTypes.has(kind)) input.append(new Option("是 / true", "true"), new Option("否 / false", "false"));
    else if (menu.length) {
      const options = new Set([...menu, values[column.eng_name] == null ? "" : String(values[column.eng_name])]);
      for (const item of options) input.append(new Option(item, item));
    }
    input.value = values[column.eng_name] == null ? "" : objectTypes.has(kind) ? JSON.stringify(values[column.eng_name]) : String(values[column.eng_name]);
    input.disabled = state.dialog.mode === "edit" && column.primary_key;
    label.append(input);
    if (!column.primary_key) {
      const nullLabel = node("label", null, "check-label"), checkbox = node("input"); checkbox.type = "checkbox";
      checkbox.dataset.nullField = column.eng_name; checkbox.checked = values[column.eng_name] == null; input.disabled = checkbox.checked;
      checkbox.onchange = () => { input.disabled = checkbox.checked; }; nullLabel.append(checkbox, node("span", "空值 NULL")); label.append(nullLabel);
    }
    el("row-fields").append(label);
  }
}
function readRowFields() {
  const result = {};
  for (const column of state.dialog.definition.columns) {
    const input = [...el("row-fields").querySelectorAll("[data-field]")].find((field) => field.dataset.field === column.eng_name);
    const nullable = [...el("row-fields").querySelectorAll("[data-null-field]")].find((field) => field.dataset.nullField === column.eng_name);
    const kind = column.data_type.toLowerCase(); let value = input.value;
    if (nullable?.checked) value = null;
    else if (integerTypes.has(kind)) { if (!/^-?\d+$/.test(value.trim())) throw new Error(`${column.eng_name} 必须是整数`); value = value.trim(); }
    else if (floatTypes.has(kind)) { if (!value.trim() || !Number.isFinite(Number(value))) throw new Error(`${column.eng_name} 必须是有限数值`); value = Number(value); }
    else if (boolTypes.has(kind)) value = value === "true";
    else if (objectTypes.has(kind)) value = JSON.parse(value);
    result[column.eng_name] = value;
  }
  return result;
}
function showSaveWarning(error) {
  const message = error.message || String(error);
  el("save-warning-list").replaceChildren(...message.split("\n").filter(Boolean).map((line) => node("li", line)));
  if (!el("save-warning-dialog").open) el("save-warning-dialog").showModal();
  notice("保存检查未通过，请修正提示的问题后再保存", true); log(message, true);
}
async function saveValidatedEdits(payload, table = state.table) {
  try {
    await request(tablePath("validate-edits", table), "POST", payload);
    return await request(tablePath("cell-edits", table), "PUT", payload);
  } catch (error) { showSaveWarning(error); return null; }
}
async function saveRow() {
  let values;
  try {
    values = el("json-mode").checked ? JSON.parse(el("row-editor").value) : readRowFields();
    if (!values || Array.isArray(values) || typeof values !== "object") throw new Error("记录须为 JSON 对象");
  } catch (error) { showSaveWarning(error); return; }
  const dialog = state.dialog;
  let payload;
  if (["edit", "batch"].includes(dialog.mode)) {
    const keys = dialog.definition.columns.filter((column) => column.primary_key).map((column) => column.eng_name);
    if (dialog.mode === "edit") for (const key of keys) {
      if (key in values && String(values[key]) !== String(dialog.records[0].row[key])) throw new Error("编辑不能更改主键，请使用“复制选中行”和“插入复制行”");
      delete values[key];
    }
    payload = { changes: dialog.records.map((record) => ({ ref: record.ref, values })) };
  } else payload = { inserts: [values] };
  if (!await saveValidatedEdits(payload, dialog.table)) return;
  el("row-dialog").close(); log(`${el("row-dialog-title").textContent}：${dialog.table}`); notice("数据已保存"); await queryRows();
}
function pendingEditPayload() {
  const groups = new Map();
  for (const edit of state.pendingEdits.values()) {
    if (edit.record.draftId) continue;
    const key = recordKey(edit.record); if (!groups.has(key)) groups.set(key, { record: edit.record, values: {} });
    groups.get(key).values[edit.column.eng_name] = parseCellValue(edit.column, edit.raw);
  }
  const inserts = draftRecords().map((record, index) => {
    const values = {};
    for (const column of state.definition.columns) {
      const pending = pendingCell(record, column.eng_name), raw = pending ? pending.raw : cellText(record.row[column.eng_name]);
      try { values[column.eng_name] = pending ? parseCellValue(column, raw) : record.row[column.eng_name]; }
      catch (error) { throw new Error(`新增第 ${index + 1} 行：${error.message}`); }
    }
    return values;
  });
  return { changes: [...groups.values()].map((group) => ({ ref: group.record.ref, values: group.values })), inserts };
}
async function saveCellEdits() {
  if (!hasPendingData()) return;
  let payload;
  try { payload = pendingEditPayload(); } catch (error) { showSaveWarning(error); return; }
  const result = await saveValidatedEdits(payload);
  if (!result) return;
  state.records = state.records.filter((record) => !record.draftId);
  state.pendingEdits.clear();
  updateEditActions();
  const message = result.inserted ? `已新增 ${result.inserted} 条记录${result.updated ? `，修改 ${result.updated} 条记录` : ""}` : `已保存 ${result.updated} 条记录`;
  log(`${state.table}：${message}`); notice(message); await queryRows();
}
async function discardCellEdits() {
  if (!hasPendingData()) return true;
  if (!await confirmAction("撤销修改", `放弃当前页面 ${draftRecords().length} 条待新增记录和 ${state.pendingEdits.size} 个单元格的修改？`)) return false;
  state.records = state.records.filter((record) => !record.draftId);
  for (const record of state.records) {
    if (!record.baseline) continue;
    record.row = structuredClone(record.baseline);
  }
  state.selected.clear(); state.selectedColumns.clear(); state.selectedCells.clear();
  state.pendingEdits.clear(); rebindAddedChartSeries(); updateEditActions(); renderGrid(); drawChart(); notice("已撤销待新增记录和单元格修改");
  return true;
}
function schemaFormValue(withMeta = false) {
  const head = { eng_name: el("schema-name").value.trim(), chn_name: el("schema-label").value.trim() };
  head.chn_name = chineseName(head);
  return { head,
    columns: [...el("schema-fields").querySelectorAll("tbody tr")].map((row) => {
      const column = Object.fromEntries([...row.querySelectorAll("[data-property]")].map((input) => [input.dataset.property, input.type === "checkbox" ? input.checked : input.value]));
      column.chn_name = chineseName(column);
      const rawDefault = column.default_value;
      delete column.default_value;
      if (rawDefault.trim()) {
        try {
          const kind = columnKind(column);
          const value = !integerTypes.has(kind) && !floatTypes.has(kind) && !boolTypes.has(kind) && !objectTypes.has(kind) && rawDefault.trim().startsWith('"')
            ? JSON.parse(rawDefault) : parseCellValue(column, rawDefault);
          if (column.primary_key && (value == null || typeof value === "string" && !value.trim())) throw new Error("主键默认值不能为空");
          column.default_value = value;
        } catch (error) { throw new Error(`${column.eng_name} 默认值：${error.message}`); }
      }
      if (withMeta && row.dataset.originalName) column.__original_name = row.dataset.originalName;
      return column;
    }) };
}
function schemaChanged() { state.schemaDirty = true; }
function schemaDefaultDisplayType(kind) {
  const type = String(kind || "str").toLowerCase();
  return integerTypes.has(type) ? "整数" : floatTypes.has(type) ? "浮点数" : "文本";
}
function updateSchemaLocks() {
  el("schema-hint").textContent = state.table
    ? "表名和字段属性均可修改；下拉框和主键单击修改，文本字段双击编辑；默认值仅用于新记录。"
    : "下拉框和主键单击修改，文本字段双击后编辑；默认值留空表示未设置，空字符串填 \"\"，对象/数组填 JSON。";
  for (const row of el("schema-fields").querySelectorAll("tbody tr")) {
    const editing = row.dataset.editing === "true";
    for (const input of row.querySelectorAll("[data-property]")) {
      const directEdit = input.tagName === "SELECT" || ["checkbox", "radio"].includes(input.type);
      input.disabled = !directEdit && !editing;
      input.title = directEdit ? "单击修改" : editing ? "编辑中" : "双击单元格进入编辑";
    }
    row.classList.toggle("schema-editing", editing);
  }
}
function renderSchema(definition) {
  el("schema-name").value = definition.head.eng_name; el("schema-label").value = chineseName(definition.head); el("schema-name").disabled = false;
  const grid = node("table"), head = node("tr"), thead = node("thead"), body = node("tbody");
  for (const label of ["英文名", "中文名", "类型", "主键", "默认值", "显示类型", "选项（| 分隔）"]) head.append(node("th", label));
  thead.append(head); grid.append(thead);
  for (const column of definition.columns) {
    const row = node("tr"); row.addEventListener("contextmenu", (event) => openSchemaContextMenu(event, row));
    let previousKind = column.data_type;
    const defaultDisplay = schemaDefaultDisplayType(previousKind);
    const displayType = !column.disp_type || (column.disp_type === "数字" && defaultDisplay !== "文本") ? defaultDisplay : column.disp_type;
    const originalName = column.__original_name || (state.table && state.definition?.columns.some((item) => item.eng_name === column.eng_name) ? column.eng_name : "");
    if (originalName) row.dataset.originalName = originalName;
    for (const property of ["eng_name", "chn_name", "data_type", "primary_key", "default_value", "disp_type", "disp_menu"]) {
      const cell = node("td"), input = node(["data_type", "disp_type"].includes(property) ? "select" : "input"); input.dataset.property = property;
      const directEdit = input.tagName === "SELECT" || ["checkbox", "radio"].includes(input.type);
      cell.title = directEdit ? "单击修改" : "双击此单元格编辑";
      input.setAttribute("aria-label", `${column.eng_name || "新字段"} ${property}`);
      if (property === "data_type") for (const kind of new Set(["int", "float", "str", "bool", "json", "list", "dict", "datetime", column.data_type])) input.append(new Option(kind, kind));
      if (property === "disp_type") for (const kind of new Set(["文本", "整数", "浮点数", "下拉框", displayType])) input.append(new Option(kind, kind));
      if (property === "primary_key") { input.type = "checkbox"; input.checked = column[property]; }
      else if (property === "default_value") {
        const value = column.default_value;
        input.value = value == null ? "" : typeof value === "string" && !integerTypes.has(columnKind(column)) ? JSON.stringify(value) : cellText(value);
        input.placeholder = "未设置";
        cell.title = '双击编辑默认值；留空不设置，空字符串填 ""，对象/数组填 JSON';
      }
      else input.value = property === "chn_name" ? chineseName(column) : property === "disp_type" ? displayType : column[property] ?? "";
      input.dataset.originalValue = input.type === "checkbox" ? String(input.checked) : input.value;
      const update = () => {
        schemaChanged();
        if (property === "data_type") {
          const displayInput = row.querySelector('[data-property="disp_type"]');
          if (displayInput.value === schemaDefaultDisplayType(previousKind)) {
            displayInput.value = schemaDefaultDisplayType(input.value);
            displayInput.closest("td").classList.toggle("schema-modified", displayInput.value !== displayInput.dataset.originalValue);
          }
          previousKind = input.value;
        }
        const current = input.type === "checkbox" ? String(input.checked) : String(input.value);
        cell.classList.toggle("schema-modified", current !== input.dataset.originalValue);
      };
      input.oninput = update; input.onchange = update;
      cell.ondblclick = (event) => {
        event.preventDefault(); event.stopPropagation();
        row.dataset.editing = "true"; updateSchemaLocks(); input.disabled = false; input.focus({ preventScroll: true });
        if (input.select && input.type !== "checkbox") input.select();
      };
      input.onblur = () => { row.dataset.editing = "false"; updateSchemaLocks(); };
      cell.append(input); row.append(cell);
    }
    body.append(row);
  }
  grid.append(body); el("schema-fields").replaceChildren(grid); updateSchemaLocks();
}
function closeSchemaContextMenu(restoreFocus = false) {
  const menu = el("schema-context-menu"), row = state.schemaContextRow;
  menu.hidden = true; state.schemaContextRow = null;
  if (restoreFocus) (row?.isConnected ? row : el("schema-fields")).focus({ preventScroll: true });
}
function openSchemaContextMenu(event, row) {
  event.preventDefault(); event.stopPropagation();
  if (state.busy) return;
  state.schemaContextRow = row;
  const menu = el("schema-context-menu"); menu.hidden = false; menu.scrollTop = 0;
  const margin = 8, width = menu.offsetWidth, height = menu.offsetHeight;
  menu.style.left = `${Math.max(margin, Math.min(event.clientX, window.innerWidth - width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(event.clientY, window.innerHeight - height - margin))}px`;
  menu.querySelector("button")?.focus({ preventScroll: true });
}
function handleSchemaContextAction(actionName) {
  const row = state.schemaContextRow; closeSchemaContextMenu();
  if (!row || actionName !== "remove-column") return;
  row.remove(); schemaChanged(); notice("字段已移除，保存结构后正式生效");
}
function closeSidebarContextMenu(restoreFocus = false) {
  const menu = el("sidebar-context-menu"), trigger = state.sidebarContextTrigger;
  menu.hidden = true; state.sidebarContextTrigger = null;
  if (restoreFocus) (trigger?.isConnected ? trigger : el("table-list")).focus({ preventScroll: true });
}
function openSidebarContextMenu(event) {
  event.preventDefault(); event.stopPropagation();
  if (state.busy) return;
  state.sidebarContextTrigger = event.target.closest("[tabindex]") || el("table-list");
  const menu = el("sidebar-context-menu");
  menu.querySelector('[data-sidebar-action="backup"]').disabled = !state.checkedTables.size;
  menu.hidden = false; menu.scrollTop = 0;
  const margin = 8, width = menu.offsetWidth, height = menu.offsetHeight;
  menu.style.left = `${Math.max(margin, Math.min(event.clientX, window.innerWidth - width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(event.clientY, window.innerHeight - height - margin))}px`;
  (menu.querySelector("button:not(:disabled)") || menu).focus({ preventScroll: true });
}
function handleSidebarContextAction(actionName) {
  closeSidebarContextMenu();
  if (actionName === "select-all-tables") {
    filteredTables().filter((item) => !item.orphan && item.selectable !== false).forEach((item) => state.checkedTables.add(item.name));
    renderTables();
  } else if (actionName === "invert-tables") {
    filteredTables().filter((item) => !item.orphan && item.selectable !== false).forEach((item) => state.checkedTables.has(item.name) ? state.checkedTables.delete(item.name) : state.checkedTables.add(item.name));
    renderTables();
  } else if (actionName === "clear-tables") {
    state.checkedTables.clear();
    renderTables();
  } else if (actionName === "select-all-fields") {
    state.fields = new Set(state.resultColumns.map((column) => column.eng_name));
    renderFields(); renderGrid();
  } else if (actionName === "invert-fields") {
    state.fields = new Set(state.resultColumns.filter((column) => column.primary_key || !state.fields.has(column.eng_name)).map((column) => column.eng_name));
    renderFields(); renderGrid();
  } else if (actionName === "clear-fields") {
    state.fields = new Set(state.resultColumns.filter((column) => column.primary_key).map((column) => column.eng_name));
    renderFields(); renderGrid();
  } else if (actionName === "load-definition") void run(openDefinitionDialog);
  else if (actionName === "backup") void run(backupSelectedTables);
  else if (actionName === "restore") chooseRestoreBackup();
}
async function saveSchema() {
  if (hasPendingData()) throw new Error("请先保存或撤销待新增记录和单元格修改，再保存表结构");
  const desired = schemaFormValue(), name = desired.head?.eng_name;
  if (!name) throw new Error("英文表名不能为空");
  const columnRenames = {};
  el("schema-fields").querySelectorAll("tbody tr[data-original-name]").forEach((row) => {
    const current = row.querySelector('[data-property="eng_name"]').value.trim();
    if (current && current !== row.dataset.originalName) columnRenames[row.dataset.originalName] = current;
  });
  const routeName = state.table || name;
  const result = await request(tablePath("schema", routeName), "PUT", { definition: desired, version: state.version,
    creating: !state.table, column_renames: columnRenames });
  const savedName = result.name || name;
  state.schemaDirty = false; await refreshTables(); await selectTable(savedName); notice(`已保存表结构：${savedName}`); log(`保存结构：${savedName}`);
}
function sqlHistoryKey() { return `rtdb.sql-history:${JSON.stringify([state.historyScope, state.table])}`; }
function normalizeSqlText(sql) { return sql.trim().replace(/;$/, "").trimEnd(); }
function loadSqlHistory() {
  state.history = []; closeSqlHistory();
  try {
    const saved = JSON.parse(localStorage.getItem(sqlHistoryKey()) || "[]");
    if (Array.isArray(saved)) state.history = [...new Set(saved.filter((item) => typeof item === "string" && !/[\r\n]/.test(item)).map(normalizeSqlText).filter(Boolean))].slice(0, 30);
  } catch { /* History remains usable without browser storage. */ }
}
function rememberSql(sql) {
  sql = normalizeSqlText(sql);
  state.history = [sql, ...state.history.filter((item) => item !== sql)].slice(0, 30);
  try { localStorage.setItem(sqlHistoryKey(), JSON.stringify(state.history)); } catch { /* Keep session history when storage is unavailable. */ }
}
function closeSqlHistory() {
  el("sql-history-list").hidden = true; el("sql-editor").setAttribute("aria-expanded", "false");
  el("sql-editor").removeAttribute("aria-activedescendant"); sqlHistoryActive = -1;
}
function openSqlHistory(filter = "") {
  const list = el("sql-history-list"), input = el("sql-editor");
  sqlHistoryItems = state.history.filter((item) => item.toLowerCase().includes(filter.trim().toLowerCase()));
  sqlHistoryActive = -1; input.removeAttribute("aria-activedescendant"); list.replaceChildren();
  sqlHistoryItems.forEach((sql, index) => {
    const option = action(sql, () => chooseSqlHistory(index), ""); option.id = `sql-history-option-${index}`;
    option.setAttribute("role", "option"); option.setAttribute("aria-selected", "false"); option.tabIndex = -1; option.title = sql;
    option.onpointerdown = (event) => event.preventDefault(); list.append(option);
  });
  if (!sqlHistoryItems.length) list.append(node("p", state.history.length ? "没有匹配的历史语句" : "执行成功后，语句会记录在这里", "muted"));
  const rect = input.getBoundingClientRect(), below = innerHeight - rect.bottom - 8, above = rect.top - 8;
  list.style.width = `${Math.min(rect.width, innerWidth - 16)}px`;
  list.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - rect.width - 8))}px`;
  list.style.maxHeight = `${Math.max(40, Math.min(240, below >= 100 || below >= above ? below : above))}px`;
  list.hidden = false; list.scrollTop = 0; input.setAttribute("aria-expanded", "true");
  list.style.top = `${below >= 100 || below >= above ? rect.bottom + 2 : rect.top - list.offsetHeight - 2}px`;
}
function chooseSqlHistory(index) {
  const sql = sqlHistoryItems[index]; if (sql === undefined) return;
  el("sql-editor").value = sql; el("sql-editor").focus({ preventScroll: true }); closeSqlHistory();
}
function singleLineSql(sql) {
  let result = "", quote = null;
  for (let index = 0; index < sql.length; index++) {
    const char = sql[index];
    if (quote) {
      if (char === "\r" || char === "\n") throw new Error("引号中的内容包含换行，无法直接放入单行 SQL 输入框");
      result += char;
      if (char === quote) { if (sql[index + 1] === quote && quote !== "]") result += sql[++index]; else quote = null; }
    } else if (char === "'" || char === '"' || char === "`" || char === "[") {
      quote = char === "[" ? "]" : char; result += char;
    } else if (char === "-" && sql[index + 1] === "-") {
      while (index < sql.length && sql[index] !== "\n" && sql[index] !== "\r") index++;
      result += " ";
    } else if (char === "/" && sql[index + 1] === "*") {
      const end = sql.indexOf("*/", index + 2); if (end < 0) throw new Error("SQL 注释尚未闭合");
      index = end + 1; result += " ";
    } else result += char === "\r" || char === "\n" ? " " : char;
  }
  return result;
}
async function executeSql() {
  closeSqlHistory();
  requireTable(); if (hasPendingData()) throw new Error("请先保存或撤销待新增记录和单元格修改，再执行 SQL"); const sql = normalizeSqlText(el("sql-editor").value);
  if (!sql) throw new Error("请输入 SQL");
  el("sql-editor").value = sql;
  const keyword = sql.replace(/^(?:\s+|--[^\n]*(?:\n|$)|\/\*[\s\S]*?\*\/)*/, "").match(/^\w+/)?.[0].toUpperCase();
  if (["SELECT", "WITH"].includes(keyword)) {
    await queryRows({ sql, order: null, reset: true }); rememberSql(sql); notice("");
    return;
  }
  if (!["INSERT", "UPDATE", "DELETE"].includes(keyword)) throw new Error("支持 SELECT / WITH 查询及 INSERT / UPDATE / DELETE 写入");
  if (!await confirmAction("执行写 SQL", `将在 ${state.table} 执行：\n${sql}`, state.table)) return;
  const result = await request(tablePath("sql"), "POST", { sql, confirm: state.table });
  rememberSql(sql); log(`SQL ${state.table}：${sql}；影响 ${result.rowcount} 条`);
  await queryRows({ sql: defaultSql(), order: null, reset: true });
  notice(`SQL 执行成功，影响 ${result.rowcount} 条 · ${result.elapsed_ms} ms`);
}
function numericColumns() {
  return state.resultColumns.filter((column) => integerTypes.has(columnKind(column)) || floatTypes.has(columnKind(column)));
}
function configureChart() {
  const columns = numericColumns(), valid = new Set(columns.map((column) => column.eng_name));
  const selectedNumeric = [...state.selectedColumns].filter((field) => valid.has(field));
  state.chartFields = new Set((selectedNumeric.length ? selectedNumeric : [...state.chartFields]).filter((field) => valid.has(field)));
  if (!state.chartFields.size) {
    const defaults = columns.filter((column) => !column.primary_key).slice(0, 4);
    (defaults.length ? defaults : columns.slice(0, 4)).forEach((column) => state.chartFields.add(column.eng_name));
  }
  el("chart-mode").value = state.chartMode;
  el("chart-x").replaceChildren(new Option("key / 行序号", ""));
  for (const column of state.resultColumns) el("chart-x").append(new Option(`${chineseName(column)} / ${column.eng_name}`, column.eng_name));
  el("chart-fields").replaceChildren();
  for (const column of columns) {
    const label = node("label"), input = node("input"); input.type = "checkbox"; input.checked = state.chartFields.has(column.eng_name);
    input.setAttribute("aria-label", `曲线字段 ${column.eng_name}`); input.dataset.field = column.eng_name;
    input.onchange = () => { input.checked ? state.chartFields.add(column.eng_name) : state.chartFields.delete(column.eng_name); resetChartCursor(); drawChart(); };
    label.append(input, node("span", chineseName(column))); el("chart-fields").append(label);
  }
}
function chartRowIndexes() {
  const indexes = state.selected.size ? [...state.selected] : state.selectedCells.size ? [...new Set([...state.selectedCells].map((key) => splitCellSelectionKey(key).index))] : state.records.map((_record, index) => index);
  return indexes.filter((index) => state.records[index]).sort((left, right) => left - right);
}
function resetChartCursor() { state.chartCursor.index = -1; state.chartCursor.pinned = false; }
function chartKeyLabel(record, index) {
  const keys = state.definition?.columns.filter((column) => column.primary_key).map((column) => record.row[column.eng_name]).filter((value) => value != null && String(value) !== "") || [];
  return keys.length ? keys.map(display).join(" / ") : `第 ${state.offset + index + 1} 行`;
}
function chartStyle(key, index = 0) {
  const color = colors[index % colors.length], saved = state.chartStyles.get(key) || {};
  return { name: "", lineColor: color, lineStyle: "solid", lineWidth: 2,
    pointColor: color, pointShape: "circle", pointSize: 3.5, ...saved };
}
function chartNumber(value) {
  if (value == null || value === "") return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}
function chartSelectedFields() {
  const selected = state.selectedColumns.size ? [...state.selectedColumns] : state.selectedCells.size ? [...new Set([...state.selectedCells].map((key) => splitCellSelectionKey(key).field))] : [...state.chartFields];
  return state.resultColumns.filter((column) => selected.includes(column.eng_name) && (integerTypes.has(columnKind(column)) || floatTypes.has(columnKind(column))));
}
function chartColumnLabel(column) { return el("chinese-columns").checked ? chineseName(column) : column.eng_name; }
function preferFloatChartPoints(points) {
  const hasFloat = points.some((point) => point.selected && floatTypes.has(columnKind(point.column)));
  return hasFloat ? points.filter((point) => !integerTypes.has(columnKind(point.column))) : points;
}
function chartSelection() {
  const selectedFields = state.selectedColumns.size ? new Set(state.selectedColumns) : state.selectedCells.size ? new Set([...state.selectedCells].map((key) => splitCellSelectionKey(key).field)) : null;
  const columns = visibleGridColumns().filter((column) => !selectedFields || selectedFields.has(column.eng_name));
  const indexes = chartRowIndexes(), fields = columns.filter((column) => integerTypes.has(columnKind(column)) || floatTypes.has(columnKind(column)));
  return { indexes, fields, cells: new Set(state.selectedCells), columnCount: columns.length, mode: indexes.length > columns.length ? "columns" : "rows" };
}
function buildSelectionChartModel({ fields = chartSelectedFields(), indexes = chartRowIndexes(), mode = el("chart-mode").value || state.chartMode, cells = state.selectedCells } = {}) {
  const point = (record, rowIndex, column, label) => {
    const selected = !cells.size || cells.has(cellSelectionKey(rowIndex, column.eng_name));
    return { label, selected, value: selected ? chartNumber(record.row[column.eng_name]) : null, record, rowIndex, field: column.eng_name, column };
  };
  if (!fields.length || !indexes.length) return { mode, labels: [], series: [] };
  if (mode === "rows") {
    const series = indexes.map((rowIndex, seriesIndex) => {
      const record = state.records[rowIndex], key = `row:${record.ref || record.draftId ? recordKey(record) : rowIndex}`, style = chartStyle(key, seriesIndex);
      const points = preferFloatChartPoints(fields.map((column) => point(record, rowIndex, column, chartColumnLabel(column))));
      return { key, name: style.name || chartKeyLabel(record, rowIndex), style, axis: state.chartAxes.get(key) || "left", color: style.lineColor, points };
    });
    return { mode, labels: series[0].points.map((point) => point.label), series };
  }
  const xField = el("chart-x").value;
  return { mode, labels: indexes.map((rowIndex) => { const record = state.records[rowIndex]; return xField ? display(record.row[xField]) : chartKeyLabel(record, rowIndex); }), series: fields.map((column, seriesIndex) => {
    const key = `field:${column.eng_name}`, style = chartStyle(key, seriesIndex);
    return { key, name: style.name || chartColumnLabel(column), style, axis: state.chartAxes.get(key) || "left", color: style.lineColor, points: indexes.map((rowIndex) => point(state.records[rowIndex], rowIndex, column, chartKeyLabel(state.records[rowIndex], rowIndex))) };
  }) };
}
function clearAddedChartSeries() {
  state.chartSeries.forEach((series) => state.chartAxes.delete(series.key));
  state.chartSeries = []; state.chartPinned = false; state.chartCleared = true; state.chartHidden.clear();
  state.chartFrame = null; state.chartHits = []; resetChartCursor(); drawChart();
}
function rebindAddedChartSeries() {
  const records = new Map(state.records.map((record, rowIndex) => [recordKey(record), { record, rowIndex }]));
  for (const series of state.chartSeries) for (const point of series.points) {
    const current = records.get(point.identity);
    point.record = current?.record || null; point.rowIndex = current?.rowIndex ?? -1;
  }
}
function addSelectionCurves(selection, axis) {
  state.chartCleared = false;
  const model = buildSelectionChartModel(selection);
  const series = model.series.filter((item) => item.points.some((point) => Number.isFinite(point.value)));
  if (!series.length) throw new Error("选区中没有可绘制的数值");
  for (const item of series) {
    const points = item.points.map((point) => ({ ...point, identity: point.record.ref || point.record.draftId ? recordKey(point.record) : `result:${point.rowIndex}` }));
    const key = `added:${model.mode}:${JSON.stringify(points.map((point) => [point.identity, point.field, point.selected]))}`;
    const added = { key, name: item.name, mode: model.mode, field: model.mode === "columns" ? points[0].field : null, points };
    const index = state.chartSeries.findIndex((existing) => existing.key === key);
    if (index < 0) state.chartSeries.push(added); else state.chartSeries[index] = added;
    state.chartAxes.set(key, axis);
    state.chartHidden.delete(key);
  }
  state.chartPinned = true; state.chartMode = model.mode; el("chart-mode").value = model.mode; resetChartCursor();
  chartLayout.expand(); bottomTab("chart"); drawChart();
  notice(`${selection.indexes.length} 行 × ${selection.columnCount} 列，按${model.mode === "rows" ? "行" : "列"}加入 ${series.length} 条${axis === "right" ? "右" : "左"}轴曲线`);
}
function buildChartModel() {
  if (state.chartCleared || !state.chartPinned) return { mode: state.chartMode, labels: [], series: [] };
  const series = state.chartSeries.map((item, index) => {
    const field = state.resultColumns.find((column) => column.eng_name === item.field);
    const style = chartStyle(item.key, index);
    return { key: item.key, name: style.name || (field ? chartColumnLabel(field) : item.name), style, axis: state.chartAxes.get(item.key) || "left", color: style.lineColor, mode: item.mode, points: preferFloatChartPoints(item.points.map((point) => {
      const column = state.resultColumns.find((candidate) => candidate.eng_name === point.field);
      const label = item.mode === "rows" && column ? chartColumnLabel(column) : point.record ? chartKeyLabel(point.record, point.rowIndex) : point.label;
      return { ...point, column: column || point.column, label, value: point.selected && point.record && column ? chartNumber(point.record.row[point.field]) : null };
    })) };
  });
  const first = series[0], sameLabels = !first || series.every((item) => item.mode === first.mode && JSON.stringify(item.points.map((point) => point.label)) === JSON.stringify(first.points.map((point) => point.label)));
  const length = Math.max(0, ...series.map((item) => item.points.length));
  const labels = sameLabels ? first?.points.map((point) => point.label) || [] : Array.from({ length }, (_item, index) => String(index + 1));
  series.forEach((item) => { while (item.points.length < length) item.points.push({ value: null }); });
  return { mode: sameLabels ? first?.mode || state.chartMode : "mixed", labels, series, ordinal: !sameLabels };
}
function renderChartLegend(model) {
  const target = el("chart-legend"), focused = target.contains(document.activeElement) ? document.activeElement : null;
  const focusedKey = focused?.closest(".legend-item")?.dataset.seriesKey, focusedControl = focused?.dataset.legendControl;
  const scrollLeft = target.scrollLeft, scrollTop = target.scrollTop;
  target.replaceChildren();
  model.series.forEach((series) => {
    const legend = node("span", null, "legend-item"), dot = node("span", null, "legend-dot"); dot.style.background = series.style?.lineColor || series.color;
    const visible = !state.chartHidden.has(series.key); legend.dataset.seriesKey = series.key; legend.classList.toggle("is-hidden", !visible);
    const toggle = action("", () => {
      if (state.chartHidden.has(series.key)) state.chartHidden.delete(series.key); else state.chartHidden.add(series.key);
      resetChartCursor(); drawChart();
    }, "legend-toggle");
    toggle.dataset.legendControl = "visibility"; toggle.setAttribute("aria-pressed", String(visible));
    toggle.setAttribute("aria-label", `${visible ? "隐藏" : "显示"}曲线 ${series.name}`); toggle.title = `${series.name}：点击${visible ? "隐藏" : "显示"}`;
    toggle.append(dot, node("span", series.name, "legend-name"));
    const axis = node("select"); axis.className = "chart-axis-select"; axis.setAttribute("aria-label", `${series.name}坐标轴`); axis.append(new Option("左轴", "left"), new Option("右轴", "right")); axis.value = series.axis;
    axis.dataset.legendControl = "axis";
    axis.onchange = () => { state.chartAxes.set(series.key, axis.value); drawChart(); };
    legend.append(toggle, axis);
    if (state.chartPinned) {
      const remove = action("×", () => { state.chartSeries = state.chartSeries.filter((item) => item.key !== series.key); state.chartAxes.delete(series.key); state.chartHidden.delete(series.key); resetChartCursor(); drawChart(); }, "secondary small");
      remove.dataset.legendControl = "remove";
      remove.setAttribute("aria-label", `移除曲线 ${series.name}`); remove.title = `移除${series.mode === "rows" ? "行" : "列"}曲线 ${series.name}`; legend.append(remove);
    }
    target.append(legend);
  });
  if (focusedKey && focusedControl) [...target.children].find((item) => item.dataset.seriesKey === focusedKey)?.querySelector(`[data-legend-control="${focusedControl}"]`)?.focus({ preventScroll: true });
  target.scrollLeft = scrollLeft; target.scrollTop = scrollTop;
}
function configuredNumber(value) {
  return value !== "" && value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value) : null;
}
function chartScale(values, settings = {}) {
  const finite = values.filter(Number.isFinite); if (!finite.length) return null;
  const configuredMin = configuredNumber(settings.min), configuredMax = configuredNumber(settings.max);
  let min = configuredMin ?? Math.min(...finite), max = configuredMax ?? Math.max(...finite);
  if (min === max) {
    const padding = Math.max(1, Math.abs(min) * .08);
    if (configuredMin === null) min -= padding;
    if (configuredMax === null) max += padding;
    if (min === max) max = min + 1;
  } else {
    const padding = (max - min) * .06;
    if (configuredMin === null) min -= padding;
    if (configuredMax === null) max += padding;
  }
  return { min, max };
}
function chartY(value, scale, top, plotHeight) { return top + (scale.max - value) / (scale.max - scale.min) * plotHeight; }
function formatChartValue(value, column) {
  if (integerTypes.has(columnKind(column))) return String(Math.round(value));
  return String(Number(value.toPrecision(8)));
}
function chartLineDash(style) {
  return style.lineStyle === "dashed" ? [8, 5] : style.lineStyle === "dotted" ? [2, 4] : style.lineStyle === "dashdot" ? [8, 4, 2, 4] : [];
}
function drawChartPoint(context, x, y, style) {
  if (style.pointShape === "none") return;
  const size = Math.max(2, Number(style.pointSize) || 3.5), half = size;
  context.beginPath();
  if (style.pointShape === "square") context.rect(x - half, y - half, half * 2, half * 2);
  else if (style.pointShape === "diamond") { context.moveTo(x, y - half); context.lineTo(x + half, y); context.lineTo(x, y + half); context.lineTo(x - half, y); context.closePath(); }
  else if (style.pointShape === "triangle") { context.moveTo(x, y - half); context.lineTo(x + half, y + half); context.lineTo(x - half, y + half); context.closePath(); }
  else context.arc(x, y, half, 0, Math.PI * 2);
  context.fill();
}
function canDragChartPoint(point) {
  return !point.column.primary_key && (!integerTypes.has(columnKind(point.column)) || Number.isSafeInteger(Number(point.record.row[point.field])));
}
function chartTitleSpace() {
  return state.chartTitle ? Math.ceil(state.chartTitleSettings.fontSize * 1.3) + state.chartTitleSettings.gap + 8 : 0;
}
function chartMinimumHeight() {
  if (!state.chartTitle || el("chart").hidden || el("tab-chart").hidden) return 170;
  const controls = el("bottom-panel").querySelector(".bottom-bar").getBoundingClientRect().height + el("tab-chart").querySelector(".chart-heading").getBoundingClientRect().height + 16;
  return Math.max(170, Math.ceil(controls + chartTitleSpace() + 76 + 60));
}
function drawChartTitle(context, width, height) {
  if (!state.chartTitle) return;
  const settings = state.chartTitleSettings, [vertical, align] = settings.position.split("-"), lineHeight = Math.ceil(settings.fontSize * 1.3);
  context.save();
  context.font = `${settings.fontStyle.includes("italic") ? "italic " : ""}${settings.fontStyle.includes("bold") ? "bold " : ""}${settings.fontSize}px "${settings.fontFamily}", "Microsoft YaHei", sans-serif`;
  context.fillStyle = settings.color; context.textAlign = align; context.textBaseline = "middle";
  const metrics = context.measureText(state.chartTitle), availableWidth = Math.max(1, width - 24);
  const scale = Math.min(1, availableWidth / Math.max(1, metrics.width, metrics.actualBoundingBoxLeft + metrics.actualBoundingBoxRight));
  const anchor = align === "left" ? 12 : align === "right" ? width - 12 : width / 2;
  const x = Math.max(12 + metrics.actualBoundingBoxLeft * scale, Math.min(anchor, width - 12 - metrics.actualBoundingBoxRight * scale));
  context.fillText(state.chartTitle, x, vertical === "top" ? 4 + lineHeight / 2 : height - 4 - lineHeight / 2, Math.max(1, metrics.width * scale));
  context.restore();
}
function drawChart() {
  if (!el("tab-chart").classList.contains("active") || document.documentElement.classList.contains("chart-collapsed")) return;
  const canvas = el("chart"), context = canvas.getContext("2d"), model = buildChartModel();
  for (const control of [el("chart-mode").closest("label"), el("chart-x").closest("label"), el("chart-fields")]) control.hidden = state.chartPinned;
  el("follow-chart-selection").hidden = el("chart-series-summary").hidden = !state.chartPinned;
  el("chart-series-summary").textContent = `已添加 ${model.series.length} 条曲线${model.ordinal ? " · 横轴：点序号（游标显示各点标签）" : ""} · 右键选区继续添加`;
  el("chart-series-summary").title = el("chart-series-summary").textContent;
  renderChartLegend(model);
  const visibleSeries = model.series.filter((series) => !state.chartHidden.has(series.key));
  const values = visibleSeries.flatMap((series) => series.points.map((point) => point.value)).filter(Number.isFinite);
  const available = visibleSeries.length && model.labels.length && values.length;
  el("chart-empty").textContent = model.series.length && !visibleSeries.length ? "全部曲线已隐藏，点击右上角图例恢复显示。" : "选中表格数据，右键选择“左轴曲线”或“右轴曲线”添加曲线。";
  el("chart-empty").hidden = Boolean(available); canvas.hidden = !available;
  if (!available) { state.chartFrame = null; state.chartHits = []; return; }
  // Keep the chosen font size and a usable plot when a large title needs more height.
  if (state.chartTitle && canvas.clientHeight < chartTitleSpace() + 136) chartLayout.expand();
  const width = canvas.clientWidth, height = canvas.clientHeight, ratio = devicePixelRatio || 1;
  if (!width || !height) return;
  canvas.width = width * ratio; canvas.height = height * ratio; context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.fillStyle = "white"; context.fillRect(0, 0, width, height); context.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
  const titleSpace = chartTitleSpace();
  const titleAtTop = state.chartTitleSettings.position.startsWith("top-");
  const left = 68, right = 68, top = 30 + (titleAtTop ? titleSpace : 0), bottom = 46 + (titleAtTop ? 0 : titleSpace), plotWidth = Math.max(1, width - left - right), plotHeight = Math.max(1, height - top - bottom);
  const ranges = state.chartDrag?.ranges || { left: chartScale(visibleSeries.filter((series) => series.axis === "left").flatMap((series) => series.points.map((point) => point.value)), state.chartAxisSettings.left), right: chartScale(visibleSeries.filter((series) => series.axis === "right").flatMap((series) => series.points.map((point) => point.value)), state.chartAxisSettings.right) };
  if (!ranges.left && ranges.right) ranges.left = ranges.right;
  if (!ranges.right && ranges.left) ranges.right = ranges.left;
  const pointX = (index) => left + index / Math.max(1, model.labels.length - 1) * plotWidth;
  const pointY = (value, axis) => chartY(value, ranges[axis] || ranges.left, top, plotHeight);
  state.chartFrame = { model: { ...model, series: visibleSeries }, labels: model.labels, left, right, top, bottom, plotWidth, plotHeight, width, height, ranges, xAt: pointX, yAt: pointY };
  context.strokeStyle = "#e8edf4"; context.lineWidth = 1;
  for (let tick = 0; tick <= 5; tick++) {
    const value = ranges.left.min + (ranges.left.max - ranges.left.min) * tick / 5, y = pointY(value, "left");
    if (state.chartAxisSettings.left.showGrid !== false && state.chartAxisSettings.right.showGrid !== false) { context.beginPath(); context.moveTo(left, y); context.lineTo(width - right, y); context.stroke(); }
    context.fillStyle = "#718198"; context.textAlign = "right"; context.fillText(Number(value.toPrecision(6)).toString(), left - 9, y + 4);
    if (ranges.right) { const rightValue = ranges.right.min + (ranges.right.max - ranges.right.min) * tick / 5; context.textAlign = "left"; context.fillText(Number(rightValue.toPrecision(6)).toString(), width - right + 9, y + 4); }
  }
  context.strokeStyle = "#b8c5d5"; context.beginPath(); context.moveTo(left, top); context.lineTo(left, height - bottom); context.lineTo(width - right, height - bottom); context.lineTo(width - right, top); context.stroke();
  context.fillStyle = "#6e7f95"; context.textAlign = "center";
  const labelStep = Math.max(1, Math.round(Number(state.chartAxisSettings.x.labelStep) || 1));
  const ticks = [...new Set([...Array.from({ length: model.labels.length }, (_unused, index) => index).filter((index) => index % labelStep === 0), Math.max(0, model.labels.length - 1)])];
  ticks.forEach((index) => context.fillText(String(model.labels[index]).slice(0, 18), pointX(index), height - bottom + 26));
  context.textAlign = "center"; if (state.chartAxisSettings.x.title) context.fillText(state.chartAxisSettings.x.title, left + plotWidth / 2, height - bottom + 42);
  drawChartTitle(context, width, height);
  context.textAlign = "left"; context.fillText(state.chartAxisSettings.left.title || "左轴", 7, top - 12); context.textAlign = "right"; context.fillText(state.chartAxisSettings.right.title || "右轴", width - 7, top - 12);
  state.chartHits = [];
  visibleSeries.forEach((series, seriesIndex) => {
    const style = series.style || chartStyle(series.key, seriesIndex);
    context.strokeStyle = style.lineColor || series.color; context.fillStyle = style.pointColor || style.lineColor || series.color; context.lineWidth = Math.max(1, Number(style.lineWidth) || 2); context.setLineDash(chartLineDash(style)); context.beginPath(); let connected = false;
    series.points.forEach((point, index) => {
      if (!Number.isFinite(point.value)) { connected = false; return; }
      const x = pointX(index), y = pointY(point.value, series.axis); if (connected) context.lineTo(x, y); else context.moveTo(x, y); connected = true;
      state.chartHits.push({ x, y, point, seriesKey: series.key, seriesIndex, axis: series.axis, index });
    });
    if (style.lineStyle !== "none") context.stroke();
    context.setLineDash([]); series.points.forEach((point, index) => { if (!Number.isFinite(point.value)) return; drawChartPoint(context, pointX(index), pointY(point.value, series.axis), style); });
  });
  const cursor = state.chartCursor;
  if (cursor.index >= 0 && cursor.index < model.labels.length) {
    const x = pointX(cursor.index); context.strokeStyle = "#536b8b"; context.setLineDash([4, 4]); context.beginPath(); context.moveTo(x, top); context.lineTo(x, height - bottom); context.stroke(); context.setLineDash([]);
    const rows = visibleSeries.map((series) => ({ series, point: series.points[cursor.index] })).filter(({ point }) => point && Number.isFinite(point.value));
    const lines = [`${state.chartDrag ? "拖拽" : cursor.pinned ? "已固定" : "游标"} · ${model.labels[cursor.index]}`, ...rows.map(({ series, point }) => `${series.name} [${series.axis === "right" ? "右轴" : "左轴"}]${model.ordinal ? ` ${point.label}` : ""}  ${display(point.record.row[point.field])}`)];
    const boxWidth = Math.min(310, Math.max(150, ...lines.map((line) => context.measureText(line).width + 20))), boxHeight = lines.length * 17 + 10, boxX = x + 12 + boxWidth < width ? x + 12 : x - boxWidth - 12, boxY = Math.min(Math.max(6, cursor.y || top + 8), Math.max(6, height - bottom - boxHeight));
    context.fillStyle = "#ffffffee"; context.strokeStyle = "#b9c8da"; context.fillRect(boxX, boxY, boxWidth, boxHeight); context.strokeRect(boxX, boxY, boxWidth, boxHeight);
    lines.forEach((line, lineIndex) => { context.fillStyle = lineIndex === 0 ? "#314c70" : "#5b6f88"; context.textAlign = "left"; context.fillText(line, boxX + 10, boxY + 19 + lineIndex * 17); });
  }
}
function chartCanvasPoint(event) {
  const rect = el("chart").getBoundingClientRect(); return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}
function nearestChartHit(x, y, seriesKey = null) {
  const hits = state.chartHits.filter((hit) => !seriesKey || hit.seriesKey === seriesKey); if (!hits.length) return null;
  return hits.reduce((nearest, hit) => Math.hypot(hit.x - x, hit.y - y) < Math.hypot(nearest.x - x, nearest.y - y) ? hit : nearest);
}
function chartValueAt(y, axis) {
  const frame = state.chartFrame, scale = frame?.ranges[axis] || frame?.ranges.left; if (!frame || !scale) return null;
  const bounded = Math.max(frame.top, Math.min(frame.top + frame.plotHeight, y)); return scale.max - (bounded - frame.top) / frame.plotHeight * (scale.max - scale.min);
}
function rememberDragChange(hit) {
  const key = cellKey(hit.point.record, hit.point.field), drag = state.chartDrag;
  if (!drag.changes.has(key)) drag.changes.set(key, { record: hit.point.record, column: hit.point.column, before: hit.point.record.row[hit.point.field], pending: pendingCell(hit.point.record, hit.point.field)?.raw });
  return key;
}
function updateChartDrag(event) {
  const drag = state.chartDrag, frame = state.chartFrame; if (!drag || !frame) return;
  if (event.pointerId !== drag.pointerId) return;
  const { x, y } = chartCanvasPoint(event);
  const hits = state.chartHits.filter((hit) => hit.seriesKey === drag.seriesKey);
  if (!hits.length) return;
  const nearest = hits.reduce((a, b) => Math.abs(a.x - x) <= Math.abs(b.x - x) ? a : b);
  const previous = drag.previous || { x, y };
  const crossed = hits.filter((hit) => hit.x >= Math.min(x, previous.x) && hit.x <= Math.max(x, previous.x));
  if (!crossed.includes(nearest)) crossed.push(nearest);
  for (const hit of crossed) {
    if (!canDragChartPoint(hit.point)) continue;
    const fraction = x === previous.x ? 1 : Math.max(0, Math.min(1, (hit.x - previous.x) / (x - previous.x)));
    const value = chartValueAt(previous.y + (y - previous.y) * fraction, hit.axis); if (!Number.isFinite(value)) continue;
    if (integerTypes.has(columnKind(hit.point.column)) && !Number.isSafeInteger(Math.round(value))) continue;
    rememberDragChange(hit); setPendingCell(hit.point.record, hit.point.column, formatChartValue(value, hit.point.column)); syncGridCell(hit.point.record, hit.point.field);
  }
  drag.previous = { x, y }; drag.moved = drag.changes.size > 0; state.chartCursor.index = nearest.index; state.chartCursor.y = y; drawChart();
}
function finishChartDrag(cancel = false) {
  const drag = state.chartDrag; if (!drag) return; state.chartDrag = null;
  if (cancel) for (const change of drag.changes.values()) {
    change.record.row[change.column.eng_name] = change.before;
    const key = cellKey(change.record, change.column.eng_name); if (change.pending === undefined) state.pendingEdits.delete(key); else state.pendingEdits.set(key, { record: change.record, column: change.column, raw: change.pending });
    syncGridCell(change.record, change.column.eng_name);
  }
  updateEditActions(); drawChart();
  if (drag.moved && !cancel) notice(`已拖拽修改 ${drag.changes.size} 个曲线点，保存后写入数据库`);
}
function chartPointerMove(event) {
  if (state.chartDrag) { updateChartDrag(event); return; }
  if (state.chartCursor.pinned) return;
  const frame = state.chartFrame; if (!frame) return; const { x, y } = chartCanvasPoint(event);
  if (x < frame.left || x > frame.left + frame.plotWidth || y < frame.top || y > frame.top + frame.plotHeight) { if (!state.chartCursor.pinned) { state.chartCursor.index = -1; drawChart(); } return; }
  const index = frame.labels.length <= 1 ? 0 : Math.max(0, Math.min(frame.labels.length - 1, Math.round((x - frame.left) / frame.plotWidth * (frame.labels.length - 1))));
  state.chartCursor.index = index; state.chartCursor.y = y; drawChart();
}
function chartPointerDown(event) {
  const frame = state.chartFrame; if (!frame || event.button !== 0) return;
  const { x, y } = chartCanvasPoint(event), hit = nearestChartHit(x, y);
  if (hit && state.editable && canDragChartPoint(hit.point) && Math.hypot(hit.x - x, hit.y - y) <= 12) {
    event.preventDefault(); state.chartDrag = { pointerId: event.pointerId, seriesKey: hit.seriesKey, ranges: structuredClone(frame.ranges), changes: new Map(), moved: false, previous: { x, y } }; el("chart").setPointerCapture(event.pointerId); return;
  }
  if (x >= frame.left && x <= frame.left + frame.plotWidth && y >= frame.top && y <= frame.top + frame.plotHeight) { state.chartCursor.pinned = !state.chartCursor.pinned; state.chartCursor.index = frame.labels.length <= 1 ? 0 : Math.max(0, Math.min(frame.labels.length - 1, Math.round((x - frame.left) / frame.plotWidth * (frame.labels.length - 1)))); state.chartCursor.y = y; drawChart(); }
}
function closeChartContextMenu(restoreFocus = false) {
  const menu = el("chart-context-menu"); menu.hidden = true;
  if (restoreFocus) el("chart").focus({ preventScroll: true });
}
function openChartContextMenu(event) {
  event.preventDefault(); event.stopPropagation();
  if (state.busy) return;
  closeRowContextMenu();
  const menu = el("chart-context-menu"), hasSeries = buildChartModel().series.length > 0;
  menu.querySelector('[data-chart-action="clear-chart"]').disabled = !hasSeries;
  menu.querySelector('[data-chart-action="series-settings"]').disabled = !hasSeries;
  menu.hidden = false; menu.scrollTop = 0;
  const margin = 8, width = menu.offsetWidth, height = menu.offsetHeight;
  menu.style.left = `${Math.max(margin, Math.min(event.clientX, window.innerWidth - width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(event.clientY, window.innerHeight - height - margin))}px`;
  (menu.querySelector("button:not(:disabled)") || menu).focus({ preventScroll: true });
}
function clearChartDisplay() {
  state.chartSeries.forEach((series) => state.chartAxes.delete(series.key));
  state.chartSeries = []; state.chartPinned = false; state.chartCleared = true; state.chartHidden.clear(); state.chartAxes.clear(); resetChartCursor(); drawChart();
  notice("曲线已清空"); log("清空曲线");
}
function renderChartSeriesSettings(model) {
  const target = el("chart-series-list"); target.replaceChildren();
  if (!model.series.length) { target.append(node("p", "当前没有曲线，请先在表格选区右键添加曲线。", "chart-series-empty")); return; }
  const table = node("table"), head = node("tr");
  ["曲线名", "坐标轴", "线颜色", "线型", "线宽", "点颜色", "点型", "点大小"].forEach((label) => head.append(node("th", label)));
  table.append(head);
  model.series.forEach((series, index) => {
    const style = chartStyle(series.key, index), row = node("tr"); row.dataset.seriesKey = series.key;
    const text = node("input"); text.type = "text"; text.value = style.name || series.name; text.maxLength = 120; text.dataset.setting = "name";
    const axis = node("select"); axis.dataset.setting = "axis"; axis.append(new Option("左轴", "left"), new Option("右轴", "right")); axis.value = series.axis;
    const lineColor = node("input"); lineColor.type = "color"; lineColor.value = style.lineColor; lineColor.dataset.setting = "lineColor";
    const lineStyle = node("select"); lineStyle.dataset.setting = "lineStyle"; [["实线", "solid"], ["虚线", "dashed"], ["点线", "dotted"], ["点划线", "dashdot"], ["隐藏", "none"]].forEach(([label, value]) => lineStyle.append(new Option(label, value))); lineStyle.value = style.lineStyle;
    const lineWidth = node("input"); lineWidth.type = "number"; lineWidth.min = "1"; lineWidth.max = "8"; lineWidth.step = "0.5"; lineWidth.value = style.lineWidth; lineWidth.dataset.setting = "lineWidth";
    const pointColor = node("input"); pointColor.type = "color"; pointColor.value = style.pointColor; pointColor.dataset.setting = "pointColor";
    const pointShape = node("select"); pointShape.dataset.setting = "pointShape"; [["圆点", "circle"], ["方点", "square"], ["菱形", "diamond"], ["三角形", "triangle"], ["隐藏", "none"]].forEach(([label, value]) => pointShape.append(new Option(label, value))); pointShape.value = style.pointShape;
    const pointSize = node("input"); pointSize.type = "number"; pointSize.min = "2"; pointSize.max = "12"; pointSize.step = "0.5"; pointSize.value = style.pointSize; pointSize.dataset.setting = "pointSize";
    [text, axis, lineColor, lineStyle, lineWidth, pointColor, pointShape, pointSize].forEach((control) => { const cell = node("td"); cell.append(control); row.append(cell); }); table.append(row);
  });
  target.append(table);
}
function openChartSettings(kind, axis = "left") {
  const dialog = el("chart-settings-dialog"); state.chartSettingsDialog = { kind, axis }; el("chart-settings-error").textContent = "";
  const axisMode = kind === "axis", axisSettings = state.chartAxisSettings[axis];
  dialog.dataset.mode = kind;
  el("chart-settings-title").textContent = axisMode ? `${axis === "x" ? "横轴" : axis === "left" ? "左轴" : "右轴"}设置` : kind === "title" ? "标题设置" : "曲线设置";
  el("chart-axis-settings").hidden = !axisMode; el("chart-series-settings").hidden = kind !== "series"; el("chart-title-settings").hidden = kind !== "title";
  if (axisMode) {
    el("chart-axis-settings-hint").textContent = axis === "x" ? "设置横轴标题和标签显示间隔。" : "留空表示自动计算范围；填写最小值和最大值可限制坐标轴范围。";
    el("chart-axis-title").value = axisSettings.title || "";
    el("chart-axis-min").value = axis === "x" ? "" : axisSettings.min ?? "";
    el("chart-axis-max").value = axis === "x" ? "" : axisSettings.max ?? "";
    el("chart-axis-min-label").hidden = axis === "x"; el("chart-axis-max-label").hidden = axis === "x"; el("chart-x-step-label").hidden = axis !== "x"; el("chart-axis-grid-label").hidden = axis === "x";
    el("chart-x-step").value = Math.max(1, Math.round(Number(axisSettings.labelStep) || 1)); el("chart-axis-grid").checked = axisSettings.showGrid !== false;
  } else if (kind === "title") {
    el("chart-title-input").value = state.chartTitle;
    const settings = state.chartTitleSettings;
    el("chart-title-color").value = settings.color; el("chart-title-size").value = settings.fontSize;
    el("chart-title-font").value = settings.fontFamily; el("chart-title-style").value = settings.fontStyle;
    el("chart-title-position").value = settings.position; el("chart-title-gap").value = settings.gap;
  } else renderChartSeriesSettings(buildChartModel());
  dialog.showModal();
}
function applyChartSettings() {
  const current = state.chartSettingsDialog; if (!current) return;
  try {
    if (current.kind === "axis") {
      const settings = state.chartAxisSettings[current.axis]; settings.title = el("chart-axis-title").value.trim();
      if (current.axis === "x") settings.labelStep = Math.max(1, Math.round(Number(el("chart-x-step").value) || 1));
      else {
        settings.min = configuredNumber(el("chart-axis-min").value); settings.max = configuredNumber(el("chart-axis-max").value);
        if (settings.min !== null && settings.max !== null && settings.min >= settings.max) throw new Error("坐标轴最小值必须小于最大值");
        settings.showGrid = el("chart-axis-grid").checked;
      }
    } else if (current.kind === "title") {
      const fontSize = Number(el("chart-title-size").value), gap = Number(el("chart-title-gap").value);
      if (!Number.isInteger(fontSize) || fontSize < 8 || fontSize > 72) throw new Error("标题字号须为 8～72 的整数");
      if (el("chart-title-gap").value === "" || !Number.isInteger(gap) || gap < 0 || gap > 32) throw new Error("标题间距须为 0～32 的整数");
      state.chartTitle = el("chart-title-input").value.trim();
      state.chartTitleSettings = normalizeChartTitleSettings({ fontSize, gap, color: el("chart-title-color").value,
        fontFamily: el("chart-title-font").value, fontStyle: el("chart-title-style").value, position: el("chart-title-position").value });
    } else {
      el("chart-series-list").querySelectorAll("tr[data-series-key]").forEach((row) => {
        const key = row.dataset.seriesKey, style = chartStyle(key), read = (name) => row.querySelector(`[data-setting="${name}"]`), axis = read("axis").value;
        style.name = read("name").value.trim(); style.lineColor = read("lineColor").value; style.lineStyle = read("lineStyle").value; style.lineWidth = Math.max(1, Math.min(8, Number(read("lineWidth").value) || 2)); style.pointColor = read("pointColor").value; style.pointShape = read("pointShape").value; style.pointSize = Math.max(2, Math.min(12, Number(read("pointSize").value) || 3.5));
        state.chartStyles.set(key, style); state.chartAxes.set(key, axis);
      });
    }
    const message = `${el("chart-settings-title").textContent}已应用`;
    saveChartSettings(); state.chartSettingsDialog = null; el("chart-settings-dialog").close(); resetChartCursor(); drawChart(); notice(message); log(message);
  } catch (error) { el("chart-settings-error").textContent = error.message; }
}
function handleChartContextAction(actionName) {
  closeChartContextMenu();
  if (actionName === "clear-chart") clearChartDisplay();
  else if (actionName === "axis-left") openChartSettings("axis", "left");
  else if (actionName === "axis-right") openChartSettings("axis", "right");
  else if (actionName === "axis-x") openChartSettings("axis", "x");
  else if (actionName === "title-settings") openChartSettings("title");
  else if (actionName === "series-settings") openChartSettings("series");
}
let definitionPreview = null, definitionRevision = 0;
function resetDefinitionPreview() {
  definitionRevision++;
  definitionPreview = null; el("apply-definition").disabled = true; el("definition-preview").textContent = "选项已更新，请预览";
}
function selectDefinitionTable(index, focus = false) {
  const host = el("definition-preview");
  const tabs = [...host.querySelectorAll(".definition-table-tab")];
  const selected = tabs[index];
  if (!selected) return;
  tabs.forEach((tab, position) => {
    const active = position === index;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    el(tab.getAttribute("aria-controls")).hidden = !active;
  });
  if (focus) {
    selected.focus({ preventScroll: true });
    selected.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
}
const definitionTypeOptions = [["int", "INT（整数）"], ["float", "FLOAT（浮点数）"], ["str", "STR（文本）"], ["bool", "BOOL（布尔）"], ["json", "JSON（对象）"], ["list", "LIST（数组）"], ["dict", "DICT（对象）"]];
function renderEfileDefinitionEditor(article, item) {
  article.dataset.definitionTable = item.name;
  article.append(node("p", "类型已根据文件全部数据行推断；默认首字段为 KEY。请确认或修改后再生成/更新结构并导入数据。", "import-warning"));
  const table = node("table", null, "definition-editor-table"), head = node("tr");
  ["字段", "中文名", "类型（按值推断）", "KEY"].forEach((label) => head.append(node("th", label))); const thead = node("thead"); thead.append(head); table.append(thead);
  const body = node("tbody");
  item.definition.columns.forEach((column) => {
    const row = node("tr"); row.dataset.definitionField = column.eng_name;
    const field = node("td", null, "definition-field-name"); field.append(node("span", column.eng_name));
    const label = node("td", chineseName(column));
    const select = node("select"); select.dataset.definitionType = "true"; select.setAttribute("aria-label", `${column.eng_name} 类型`);
    const options = definitionTypeOptions.some((option) => option[0] === column.data_type) ? definitionTypeOptions : [...definitionTypeOptions, [column.data_type, `${column.data_type}（当前）`]];
    options.forEach(([value, text]) => select.append(new Option(text, value))); select.value = column.data_type;
    const typeCell = node("td"); typeCell.append(select);
    const keyCell = node("td"); const key = node("input"); key.type = "checkbox"; key.checked = Boolean(column.primary_key); key.dataset.definitionKey = "true"; key.setAttribute("aria-label", `${column.eng_name} KEY`); keyCell.append(key);
    row.append(field, label, typeCell, keyCell); body.append(row);
  });
  table.append(body); article.append(table);
}
function collectDefinitionOverrides() {
  if (!definitionPreview || definitionPreview.format !== "e") return undefined;
  const overrides = {};
  el("definition-preview").querySelectorAll("article[data-definition-table]").forEach((article, index) => {
    const columns = [...article.querySelectorAll("tr[data-definition-field]")].map((row) => ({
      eng_name: row.dataset.definitionField,
      data_type: row.querySelector("[data-definition-type]").value,
      primary_key: row.querySelector("[data-definition-key]").checked,
    }));
    if (!columns.some((column) => column.primary_key)) {
      selectDefinitionTable(index, true);
      throw new Error(`${article.dataset.definitionTable}：至少选择一个 KEY 字段`);
    }
    overrides[article.dataset.definitionTable] = { columns };
  });
  return overrides;
}
async function previewDefinition() {
  const file = el("definition-file").files[0];
  if (!file || !/\.(dbdef|e)$/i.test(file.name)) throw new Error("请选择 .dbdef 或 .e 文件");
  if (!file.size || file.size > 20 * 1024 * 1024) throw new Error("请选择不超过 20 MiB 的非空文件");
  resetDefinitionPreview();
  const revision = definitionRevision;
  const content = await new Promise((resolve, reject) => {
    const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = () => reject(new Error("文件读取失败")); reader.readAsDataURL(file);
  });
  const payload = { content, source_name: file.name, format: file.name.toLowerCase().endsWith(".dbdef") ? "dbdef" : "e", encoding: el("definition-encoding").value, mode: el("definition-mode").value, keys: el("definition-keys").value };
  const result = await request("definition-preview", "POST", payload);
  if (revision !== definitionRevision) throw new Error("文件或选项已改变，请重新预览");
  const host = el("definition-preview"); host.replaceChildren(node("p", `${file.name} · ${result.encoding} · ${result.tables.length} 张表`));
  const tabs = node("nav", null, "tabs definition-table-tabs");
  tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "导入表预览");
  host.append(tabs);
  for (const [index, item] of result.tables.entries()) {
    const tab = action(item.name, () => selectDefinitionTable(index), "tab definition-table-tab");
    tab.id = `definition-table-tab-${index}`; tab.title = item.name;
    tab.setAttribute("role", "tab"); tab.setAttribute("aria-controls", `definition-table-panel-${index}`);
    tab.onkeydown = (event) => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % result.tables.length;
      else if (event.key === "ArrowLeft") next = (index + result.tables.length - 1) % result.tables.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = result.tables.length - 1;
      else return;
      event.preventDefault(); selectDefinitionTable(next, true);
    };
    tabs.append(tab);
    const article = node("article", null, "definition-table-panel");
    article.id = `definition-table-panel-${index}`; article.tabIndex = 0;
    article.setAttribute("role", "tabpanel"); article.setAttribute("aria-labelledby", tab.id);
    article.append(node("strong", `${item.exists ? "更新" : "新建"} ${item.name} · ${item.before} → ${item.after} 条`));
    article.append(node("p", `文件 ${item.incoming} 条，新增 ${item.inserted} 条，更新 ${item.updated} 条，跳过 ${item.skipped || 0} 条；文件内重复 ${item.duplicates || 0} 条；主键：${item.primary_keys.join(", ")}`));
    article.append(node("p", `字段：新增 ${item.added.join(", ") || "无"}；删除 ${item.removed.join(", ") || "无"}；变更 ${item.changed.join(", ") || "无"}`));
    item.warnings.forEach((warning) => article.append(node("p", warning, "import-warning")));
    if (payload.format === "e") renderEfileDefinitionEditor(article, item);
    else {
      const details = node("details"); details.append(node("summary", "查看字段与定义文件"));
      const table = node("table"), head = node("tr"); ["字段", "中文名", "类型", "主键"].forEach((label) => head.append(node("th", label))); table.append(head);
      item.definition.columns.forEach((column) => { const row = node("tr"); [column.eng_name, chineseName(column), column.data_type, column.primary_key ? "是" : ""].forEach((value) => row.append(node("td", value))); table.append(row); });
      details.append(table, node("p", `JSON：${item.json_path}`), node("p", `Python：${item.python_path}`)); article.append(details);
    }
    host.append(article);
  }
  selectDefinitionTable(0);
  definitionPreview = { ...payload, token: result.token }; el("apply-definition").disabled = false;
}
async function applyDefinitionImport() {
  if (!definitionPreview) throw new Error("请先预览文件");
  if (hasPendingData() || state.schemaDirty) throw new Error("请先保存或撤销页面中的修改，再导入文件");
  const overrides = collectDefinitionOverrides();
  const result = await request("definition-import", "POST", { ...definitionPreview, definition_overrides: overrides });
  const message = result.results.map((item) => `${item.name}：${item.ok ? `加载成功，现有 ${item.rows} 条（新增 ${item.inserted || 0}、更新 ${item.updated || 0}、跳过 ${item.skipped || 0}；文件内重复 ${item.duplicates || 0} 条）` : item.error}`).join("；") + (result.pending.length ? `；尚未处理：${result.pending.join(", ")}` : "");
  resetDefinitionPreview();
  if (result.ok) el("definition-dialog").close(); else el("definition-error").textContent = message;
  await refreshTables();
  const target = result.results.find((item) => item.ok)?.name;
  if (target) await selectTable(target);
  notice(message, !result.ok); log(message, !result.ok);
}
async function importFile() {
  requireTable(); if (hasPendingData()) throw new Error("请先保存或撤销待新增记录和单元格修改，再导入数据"); const file = el("import-file").files[0]; if (!file) throw new Error("请选择文件");
  if (file.size > 16 * 1024 * 1024) throw new Error("页面导入文件上限为 16 MiB");
  const text = new TextDecoder(el("import-encoding").value, { fatal: true }).decode(await file.arrayBuffer());
  const format = file.name.toLowerCase().endsWith(".csv") ? "csv" : "json", mode = el("import-mode").value;
  let version;
  if (mode === "replace") version = (await request(tablePath("snapshot"))).version;
  if (mode !== "append" && !await confirmAction("确认导入", `${mode === "merge" ? "按主键合并" : "替换整表"} ${state.table}，文件：${file.name}`, state.table)) return;
  const result = await request(tablePath("import"), "POST", { format, text, mode, version, confirm: state.table });
  el("import-dialog").close(); notice(`已导入 ${result.imported} 条`); log(`导入 ${state.table}：${mode}，${result.imported} 条`); await queryRows();
}
async function restoreBackup() {
  const file = el("restore-file").files[0]; if (!file) return;
  if (file.size > 16 * 1024 * 1024) throw new Error("页面恢复文件上限为 16 MiB");
  const text = await file.text(), preview = await request("restore-preview", "POST", { text });
  const summary = preview.tables.map((item) => `${item.name}：${item.exists ? `现有 ${item.count} 条` : "新建表"} → ${item.incoming} 条`).join("；");
  if (!await confirmAction("恢复备份", `${summary}。已有表将覆盖数据；每张表单独提交，失败会停止并报告已完成的表。`, "恢复备份")) return;
  const result = await request("restore", "POST", { text, confirm: "恢复备份", versions: Object.fromEntries(preview.tables.map((item) => [item.name, item.version])) });
  const message = result.results.map((item) => `${item.name}：${item.ok ? `恢复 ${item.imported} 条` : item.error}`).join("；") + (result.pending.length ? `；尚未处理：${result.pending.join(", ")}` : "");
  await refresh(); notice(message, !result.ok); log(message, !result.ok);
}

function openDefinitionDialog() {
  if (hasPendingData() || state.schemaDirty) throw new Error("请先保存或撤销页面中的修改，再加载文件");
  el("definition-error").textContent = ""; resetDefinitionPreview(); el("definition-dialog").showModal();
}
async function backupSelectedTables() {
  if (!state.checkedTables.size) throw new Error("请在表索引中勾选要备份的表");
  const tables = [...state.checkedTables], result = await request("backup", "POST", { tables });
  download(result.text, result.filename, result.mime); log(`备份表：${tables.join(", ")}`);
}
function chooseRestoreBackup() {
  if (!state.busy) { el("restore-file").value = ""; el("restore-file").click(); }
}

const chartLayout = setupSplitter({ id: "chart-splitter", panelId: "bottom-panel", axis: "y", direction: -1, defaultSize: 240,
  minimum: chartMinimumHeight, maximum: () => {
    const workspace = document.querySelector(".workspace"), mainTabs = document.querySelector(".workspace > .tabs"), statusbar = document.querySelector(".statusbar");
    const mainPanel = el("tab-data").classList.contains("active") ? el("query-panel").getBoundingClientRect().height + el("query-splitter").getBoundingClientRect().height + 80 : 80;
    return Math.max(0, (workspace?.clientHeight || 0) - (mainTabs?.getBoundingClientRect().height || 0) - (statusbar?.getBoundingClientRect().height || 0) - mainPanel - el("chart-splitter").getBoundingClientRect().height);
  },
  collapseThreshold: 48, cssVariable: "--chart-height", collapsedClass: "chart-collapsed" });
setupSplitter({ id: "table-list-splitter", panelId: "table-list", axis: "y", defaultSize: 180,
  minimum: () => 72, maximum: () => Math.max(120, el("table-sidebar").clientHeight - 250),
  collapseThreshold: 44, cssVariable: "--table-list-height", collapsedClass: "table-list-collapsed" });
setupSplitter({ id: "sidebar-splitter", panelId: "table-sidebar", axis: "x", defaultSize: window.innerWidth <= 720 ? 0 : 244,
  minimum: () => 176, maximum: () => Math.min(480, document.querySelector(".layout").clientWidth - 320),
  collapseThreshold: 56, cssVariable: "--sidebar-width", collapsedClass: "sidebar-collapsed" });
setupSplitter({ id: "query-splitter", panelId: "query-panel", axis: "y", defaultSize: 44,
  minimum: () => 40, maximum: () => 44,
  collapseThreshold: 16, cssVariable: "--query-height", collapsedClass: "query-collapsed" });

document.querySelectorAll("[data-tab]").forEach((button) => { button.onclick = () => tab(button.dataset.tab); });
const bottomTabs = [...document.querySelectorAll("[data-bottom-tab]")];
bottomTabs.forEach((button, index) => {
  button.onclick = () => bottomTab(button.dataset.bottomTab);
  button.onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? bottomTabs.length - 1 : (index + (event.key === "ArrowLeft" ? -1 : 1) + bottomTabs.length) % bottomTabs.length;
    bottomTab(bottomTabs[next].dataset.bottomTab); bottomTabs[next].focus();
  };
});
el("row-context-menu").addEventListener("click", (event) => {
  const actionButton = event.target.closest("[data-action]"); if (actionButton && !actionButton.disabled) handleRowContextAction(actionButton.dataset.action);
});
el("schema-context-menu").addEventListener("click", (event) => {
  const actionButton = event.target.closest("[data-schema-action]"); if (actionButton && !actionButton.disabled) handleSchemaContextAction(actionButton.dataset.schemaAction);
});
el("chart-context-menu").addEventListener("click", (event) => {
  const actionButton = event.target.closest("[data-chart-action]"); if (actionButton && !actionButton.disabled) handleChartContextAction(actionButton.dataset.chartAction);
});
el("sidebar-context-menu").addEventListener("click", (event) => {
  const actionButton = event.target.closest("[data-sidebar-action]"); if (actionButton && !actionButton.disabled) handleSidebarContextAction(actionButton.dataset.sidebarAction);
});
el("chart-context-menu").addEventListener("keydown", (event) => {
  if (event.key === "Tab") { closeChartContextMenu(true); return; }
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const buttons = [...event.currentTarget.querySelectorAll("button:not(:disabled)")]; if (!buttons.length) return;
  const current = buttons.indexOf(document.activeElement);
  const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
  buttons[index].focus();
});
el("chart-area").addEventListener("contextmenu", openChartContextMenu);
el("records-panel").addEventListener("contextmenu", (event) => openRowContextMenu(event, null, null));
el("table-list").addEventListener("contextmenu", openSidebarContextMenu);
el("data-grid").addEventListener("keydown", (event) => {
  if (event.target.closest(".cell-editor") || !(event.ctrlKey || event.metaKey) || event.altKey) return;
  if (event.key.toLowerCase() !== "c") return;
  event.preventDefault(); event.stopPropagation(); void run(copySelectedCells);
});
el("data-grid").addEventListener("paste", (event) => {
  if (event.target.closest(".cell-editor")) return;
  const text = event.clipboardData?.getData("text/plain");
  if (text == null) return;
  event.preventDefault(); event.stopPropagation(); void run(() => pasteClipboardText(text));
});
el("records-panel").addEventListener("keydown", (event) => {
  if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
  if (event.target.matches("input, select, textarea")) return;
  event.preventDefault();
  const bounds = event.target.getBoundingClientRect();
  event.target.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: bounds.left + 8, clientY: bounds.top + 8 }));
});
el("table-list").addEventListener("keydown", (event) => {
  if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
  event.preventDefault();
  const bounds = event.currentTarget.getBoundingClientRect();
  event.currentTarget.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: bounds.left + 8, clientY: bounds.top + 8 }));
});
el("row-context-menu").addEventListener("keydown", (event) => {
  if (event.key === "Tab") { closeRowContextMenu(true); return; }
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const buttons = [...event.currentTarget.querySelectorAll("button:not(:disabled)")]; if (!buttons.length) return;
  const current = buttons.indexOf(document.activeElement);
  const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
  buttons[index].focus();
});
el("sidebar-context-menu").addEventListener("keydown", (event) => {
  if (event.key === "Tab") { closeSidebarContextMenu(true); return; }
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const buttons = [...event.currentTarget.querySelectorAll("button:not(:disabled)")]; if (!buttons.length) return;
  const current = buttons.indexOf(document.activeElement);
  const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
  buttons[index].focus();
});
document.addEventListener("click", (event) => { if (!event.target.closest("#row-context-menu")) closeRowContextMenu(); if (!event.target.closest("#chart-context-menu")) closeChartContextMenu(); if (!event.target.closest("#schema-context-menu")) closeSchemaContextMenu(); if (!event.target.closest("#sidebar-context-menu")) closeSidebarContextMenu(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !el("row-context-menu").hidden) { event.preventDefault(); closeRowContextMenu(true); } });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !el("chart-context-menu").hidden) { event.preventDefault(); closeChartContextMenu(true); } });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !el("schema-context-menu").hidden) { event.preventDefault(); closeSchemaContextMenu(true); } });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !el("sidebar-context-menu").hidden) { event.preventDefault(); closeSidebarContextMenu(true); } });
document.addEventListener("contextmenu", (event) => { if (!event.target.closest("#records-panel, #row-context-menu")) closeRowContextMenu(); if (!event.target.closest("#chart-area, #chart-context-menu")) closeChartContextMenu(); if (!event.target.closest("#schema-fields, #schema-context-menu")) closeSchemaContextMenu(); if (!event.target.closest("#table-list, #sidebar-context-menu")) closeSidebarContextMenu(); });
document.addEventListener("scroll", (event) => { if (!el("row-context-menu").contains(event.target)) closeRowContextMenu(); if (!el("chart-context-menu").contains(event.target)) closeChartContextMenu(); if (!el("schema-context-menu").contains(event.target)) closeSchemaContextMenu(); if (!el("sidebar-context-menu").contains(event.target)) closeSidebarContextMenu(); }, true);
document.addEventListener("pointermove", updateCellSelectionFromPointer);
document.addEventListener("pointerup", finishCellSelection);
document.addEventListener("pointercancel", finishCellSelection);
window.addEventListener("blur", finishCellSelection);
window.addEventListener("blur", () => { closeRowContextMenu(); closeChartContextMenu(); closeSchemaContextMenu(); closeSidebarContextMenu(); });
window.addEventListener("resize", () => { closeRowContextMenu(); closeChartContextMenu(); closeSchemaContextMenu(); closeSidebarContextMenu(); });
for (const id of ["table-filter", "exact-match", "nonempty"]) el(id).addEventListener("input", renderTables);
el("refresh").onclick = () => run(refresh);
el("query").onclick = () => run(executeSql);
el("page-size").onchange = () => run(() => queryRows({ reset: true }));
el("chinese-columns").onchange = () => { renderGrid(); drawChart(); };
el("prev").onclick = () => run(async () => { state.offset = Math.max(0, state.offset - Number(el("page-size").value)); await queryRows(); });
el("next").onclick = () => run(async () => { state.offset += Number(el("page-size").value); await queryRows(); });
el("go-page").onclick = () => run(async () => { const page = Number(el("page-number").value), size = Number(el("page-size").value);
  if (!Number.isInteger(page) || page < 1 || page > Math.max(1, Math.ceil(state.total / size))) throw new Error("页码超出范围"); state.offset = (page - 1) * size; await queryRows(); });
el("save-cell-edits").onclick = () => run(saveCellEdits);
el("discard-cell-edits").onclick = () => run(discardCellEdits);
el("add-rows-form").onsubmit = (event) => {
  event.preventDefault(); run(addBlankRows, "add-rows-error");
};
document.querySelectorAll("[data-add-rows-cancel]").forEach((button) => { button.onclick = () => el("add-rows-dialog").close(); });
el("save-row").onclick = () => run(saveRow, "row-error");
el("json-mode").onchange = () => {
  try { if (el("json-mode").checked) el("row-editor").value = JSON.stringify(readRowFields(), null, 2); else buildRowFields(JSON.parse(el("row-editor").value));
    el("row-fields").hidden = el("json-mode").checked; el("row-editor").hidden = !el("json-mode").checked; el("row-error").textContent = "";
  } catch (error) { el("json-mode").checked = !el("json-mode").checked; el("row-error").textContent = error.message; }
};
el("new-table").onclick = () => run(async () => {
  if (!await discardSchema() || !await discardCellEdits()) return; resetSelection();
  renderSchema({ head: { eng_name: "", chn_name: "" }, columns: [{ eng_name: "id", chn_name: "编号", data_type: "int", primary_key: true, disp_type: "整数", disp_menu: "" },
    { eng_name: "name", chn_name: "名称", data_type: "str", primary_key: false, disp_type: "文本", disp_menu: "" }] }); tab("schema");
});
async function openDatabaseFileDialog() {
  if (!await discardSchema() || !await discardCellEdits()) return;
  setDatabaseFileTab("server"); el("database-file-error").textContent = ""; el("database-file-dialog").showModal();
  await loadDatabaseFiles();
}
el("app-title").ondblclick = () => run(openDatabaseFileDialog);
el("open-database-files").onclick = () => run(openDatabaseFileDialog);
document.querySelectorAll("[data-database-file-tab]").forEach((button) => { button.onclick = () => setDatabaseFileTab(button.dataset.databaseFileTab); });
el("refresh-database-files").onclick = () => run(loadDatabaseFiles);
el("database-repair-embed").onclick = () => run(() => repairDatabase("rebuild-embedded-definition"), "database-repair-error");
el("database-repair-delete").onclick = () => run(() => repairDatabase("delete-orphan"), "database-repair-error");
el("database-repair-rebuild").onclick = () => run(() => repairDatabase("rebuild-from-embedded"), "database-repair-error");
el("database-repair-dialog").addEventListener("close", () => { state.databaseRepairFile = null; });
el("database-file-upload-input").onchange = renderDatabaseUploadSelection;
el("upload-database-file").onclick = () => run(uploadDatabaseFile, "database-file-error");
el("create-database-file").onclick = () => run(createDatabaseFile);
el("schema-name").oninput = schemaChanged; el("schema-label").oninput = schemaChanged;
el("add-column").onclick = () => {
  const definition = schemaFormValue(true), names = new Set(definition.columns.map((column) => column.eng_name));
  let number = definition.columns.length + 1;
  while (names.has(`field_${number}`)) number++;
  definition.columns.push({ eng_name: `field_${number}`, chn_name: "新字段", data_type: "str", primary_key: false, disp_type: "文本", disp_menu: "" });
  renderSchema(definition); schemaChanged();
};
el("save-schema").onclick = () => run(saveSchema);
el("export-schema").onclick = () => run(() => { const definition = schemaFormValue(); download(JSON.stringify(definition, null, 2), `${definition.head.eng_name || "table"}.schema.json`, "application/json"); });
el("check-table").onclick = () => run(async () => { requireTable(); const result = await request(tablePath("check")); const message = `${state.table}：${result.ok ? "完整性检查通过" : "检查失败"}，${result.actual_count} 条，WAL / FULL`; notice(message, !result.ok); log(message, !result.ok); });
el("delete-schema").onclick = () => run(async () => {
  requireTable(); const name = state.table;
  if (!await confirmAction("删除空表", `仅允许删除空表 ${name}，数据库与定义将归档保存。`, name)) return;
  await request(tablePath("schema"), "DELETE", { confirm: name, version: state.version }); state.schemaDirty = false; state.table = null; await refresh(); notice(`已归档删除空表：${name}`); log(`删除空表 ${name}`);
});
el("sql-history-toggle").onpointerdown = (event) => event.preventDefault();
el("sql-history-toggle").onclick = () => { if (!el("sql-history-list").hidden) closeSqlHistory(); else { el("sql-editor").focus(); openSqlHistory(); } };
el("sql-editor").onfocus = () => { if (state.history.length) openSqlHistory(); };
el("sql-editor").onblur = closeSqlHistory;
el("sql-editor").oninput = () => { if (state.history.length) openSqlHistory(el("sql-editor").value); };
el("sql-editor").onpaste = (event) => {
  const pasted = event.clipboardData?.getData("text/plain"); if (!pasted || !/[\r\n]/.test(pasted)) return;
  event.preventDefault();
  try {
    const input = el("sql-editor"), prefix = input.value.slice(0, input.selectionStart), suffix = input.value.slice(input.selectionEnd);
    const value = singleLineSql(prefix + pasted + suffix), cursor = singleLineSql(prefix + pasted).length;
    input.value = value; input.setSelectionRange(cursor, cursor); input.dispatchEvent(new Event("input"));
  }
  catch (error) { notice(error.message, true); }
};
el("sql-editor").onkeydown = (event) => {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === "Escape" || event.key === "Tab") { closeSqlHistory(); return; }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault(); if (el("sql-history-list").hidden) openSqlHistory();
    if (!sqlHistoryItems.length) return;
    sqlHistoryActive = sqlHistoryActive < 0 ? event.key === "ArrowDown" ? 0 : sqlHistoryItems.length - 1 : (sqlHistoryActive + (event.key === "ArrowDown" ? 1 : -1) + sqlHistoryItems.length) % sqlHistoryItems.length;
    el("sql-history-list").querySelectorAll("[role=option]").forEach((option, index) => option.setAttribute("aria-selected", String(index === sqlHistoryActive)));
    const option = el(`sql-history-option-${sqlHistoryActive}`); el("sql-editor").setAttribute("aria-activedescendant", option.id); option.scrollIntoView({ block: "nearest" });
  } else if (event.key === "Enter") {
    event.preventDefault();
    if (!event.ctrlKey && !event.metaKey && !el("sql-history-list").hidden && sqlHistoryActive >= 0) chooseSqlHistory(sqlHistoryActive);
    else run(executeSql);
  }
};
document.addEventListener("pointerdown", (event) => { if (!event.target.closest(".sql-combobox, #sql-history-list")) closeSqlHistory(); });
document.addEventListener("scroll", (event) => { if (event.target !== el("sql-history-list")) closeSqlHistory(); }, true);
window.addEventListener("resize", closeSqlHistory);
el("import-file").onchange = () => { const file = el("import-file").files[0]; el("import-preview").textContent = file ? `${file.name} · ${(file.size / 1024).toFixed(1)} KiB` : "尚未选择文件"; };
el("run-import").onclick = () => run(importFile, "import-error");
["definition-file", "definition-encoding", "definition-mode", "definition-keys"].forEach((id) => el(id).addEventListener("input", resetDefinitionPreview));
el("preview-definition").onclick = () => run(previewDefinition, "definition-error");
el("apply-definition").onclick = () => run(applyDefinitionImport, "definition-error");
el("restore-file").onchange = () => run(restoreBackup);
el("chart-mode").onchange = () => { state.chartMode = el("chart-mode").value; resetChartCursor(); drawChart(); };
el("chart-x").onchange = () => { resetChartCursor(); drawChart(); };
el("follow-chart-selection").onclick = clearChartDisplay;
el("apply-chart-settings").onclick = applyChartSettings;
el("chart").addEventListener("pointermove", chartPointerMove);
el("chart").addEventListener("pointerdown", chartPointerDown);
el("chart").addEventListener("pointerup", () => finishChartDrag(false));
el("chart").addEventListener("pointercancel", () => finishChartDrag(true));
el("chart").addEventListener("lostpointercapture", () => { if (state.chartDrag) finishChartDrag(false); });
el("chart").addEventListener("pointerleave", () => { if (!state.chartCursor.pinned && !state.chartDrag) { state.chartCursor.index = -1; drawChart(); } });
el("chart").addEventListener("keydown", (event) => {
  if (event.key === "Escape") { event.preventDefault(); if (state.chartDrag) finishChartDrag(true); else { state.chartCursor.index = -1; state.chartCursor.pinned = false; drawChart(); } return; }
  if (!state.chartFrame || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault(); const last = state.chartFrame.labels.length - 1, current = state.chartCursor.index < 0 ? 0 : state.chartCursor.index;
  state.chartCursor.index = event.key === "Home" ? 0 : event.key === "End" ? last : Math.max(0, Math.min(last, current + (event.key === "ArrowLeft" ? -1 : 1))); state.chartCursor.pinned = true; drawChart();
});
el("save-chart").onclick = () => run(() => { if (el("chart").hidden) throw new Error("没有可导出的曲线"); const anchor = node("a"); anchor.href = el("chart").toDataURL("image/png"); anchor.download = `${state.table}-chart.png`; anchor.click(); });
el("query-logs").onclick = () => run(async () => {
  state.logQuery = { ...state.logQuery, date: el("log-date").value, keyword: el("log-keyword").value.trim(),
    category: el("log-category").value, status: el("log-status").value,
    pageSize: Number(el("log-page-size").value), page: 1 };
  await loadAuditLogs();
});
el("log-keyword").onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); el("query-logs").click(); } };
el("refresh-logs").onclick = () => run(async () => { await loadLogConfig(); await loadAuditLogs(); });
el("log-prev").onclick = () => run(async () => { if (state.logQuery.page > 1) { state.logQuery.page -= 1; await loadAuditLogs(); } });
el("log-next").onclick = () => run(async () => { if (state.logQuery.page < state.logQuery.pages) { state.logQuery.page += 1; await loadAuditLogs(); } });
el("export-logs").onclick = () => {
  const text = state.auditRecords.map((record) => JSON.stringify(record)).join("\n");
  download(text ? `${text}\n` : "", `rtdb-operations-${state.logQuery.date || "all"}-page-${state.logQuery.page}.jsonl`, "application/x-ndjson;charset=utf-8");
};
el("save-log-config").onclick = () => run(async () => {
  const retention = Number(el("log-retention-days").value);
  if (!Number.isInteger(retention) || retention < 0 || retention > 36500) throw new Error("日志保留天数必须是 0–36500 的整数");
  const result = await request("log-config", "PUT", { retention_days: retention });
  state.logConfig = result; state.logDates = result.dates || state.logDates; state.logQuery.page = 1;
  renderLogDates(); renderLogSummary(); await loadAuditLogs();
  notice(retention ? `日志保留期已设置为 ${retention} 天` : "日志已设置为永久保留");
});
el("cleanup-logs").onclick = () => run(async () => {
  const result = await request("logs/cleanup", "POST", {});
  state.logDates = result.dates || state.logDates; state.logQuery.page = 1;
  renderLogDates(); await loadAuditLogs();
  notice(`日志清理完成：删除 ${result.deleted_files?.length || 0} 个文件、${result.deleted_records || 0} 条记录`);
});
el("auto-refresh").onchange = () => {
  clearInterval(state.timer); const seconds = Number(el("auto-refresh").value);
  if (seconds) state.timer = setInterval(() => { if (!state.busy && !document.hidden && !document.querySelector("dialog[open]") && !state.contextRecord && !state.schemaDirty && !state.selected.size && !state.selectedColumns.size && !state.selectedCells.size && !hasPendingData()) run(() => queryRows({ silent: true })); }, seconds * 1000);
};
document.addEventListener("keydown", (event) => { if (event.key === "F5") { event.preventDefault(); if (!document.querySelector("dialog[open]")) run(refresh); } });
window.addEventListener("beforeunload", (event) => { if (state.schemaDirty || hasPendingData()) { event.preventDefault(); event.returnValue = ""; } });
let chartResizeFrame;
const chartObserver = new ResizeObserver(() => {
  cancelAnimationFrame(chartResizeFrame);
  chartResizeFrame = requestAnimationFrame(drawChart);
});
chartObserver.observe(el("chart-area"));
run(async () => {
  const info = await request("info"); el("connection").textContent = `SQLite ${info.sqlite_version} · WAL / FULL`;
  state.historyScope = `${info.data_dir}\n${info.schema_dir}`;
  state.dataDir = info.data_dir || "";
  el("storage-path").textContent = info.data_dir; el("storage-path").title = `数据：${info.data_dir}\n定义：${info.schema_dir}`;
  log("管理终端已连接 SQLite"); await refreshTables(); if (state.tables.length) await selectTable(state.tables[0].name); else resetSelection();
});
