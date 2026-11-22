const el = (id) => document.getElementById(id);
const state = { table: null, tables: [], definition: null, version: null, records: [], total: 0, tableTotal: 0,
  offset: 0, fields: new Set(), selected: new Set(), checkedTables: new Set(), logs: [], history: [],
  busy: false, schemaDirty: false, timer: null, dialog: null, chartFields: new Set(), lastQuery: null,
  resultColumns: [], editable: false, pendingEdits: new Map(), draftSequence: 0, copiedRows: null, addRowIndex: null, selectedColumns: new Set(), selectedCells: new Set(),
  rowAnchor: null, columnAnchor: null, cellAnchor: null, cellDrag: null, chartMode: "rows", chartAxes: new Map(),
  chartFrame: null, chartHits: [], chartCursor: { index: -1, pinned: false, y: 0 }, chartDrag: null, contextRecord: null,
  chartSeries: [], chartPinned: false, chartCleared: true, chartQueryKey: null, chartHidden: new Set(),
  chartStyles: new Map(), chartTitle: "", chartTitleSettings: null, auditRecords: [], auditLoaded: false, logQuery: { date: "", keyword: "", category: "", status: "", page: 1, pageSize: 50, total: 0, pages: 1 },
  logConfig: null, logDates: [], chartAxisSettings: {
    left: { title: "左轴", min: null, max: null, showGrid: true },
    right: { title: "右轴", min: null, max: null, showGrid: true },
    x: { title: "", labelStep: 1 }
  }, chartSettingsDialog: null, historyScope: "", dataDir: "", schemaContextRow: null, databaseRepairFile: null, sidebarContextTrigger: null, sidebarContextScope: null };
let sqlHistoryItems = [], sqlHistoryActive = -1;
state.database = null; state.databases = []; state.queryVersion = null; state.writeSeq = 0;
let gridCells = new WeakMap(), gridCellEntries = [], gridRowEntries = [], gridColumnEntries = [];
let selectionFrame = null, chartDrawFrame = null, chartOverlayFrame = null, legendSignature = null, renderedQueryStamp = null, noticeTimer;
let chartStaticCanvas = null, chartStaticKey = null, chartStaticHits = [];
const menuDictCache = new Map();
const choiceSetCache = new Map();
function scheduleGridSelection() {
  if (selectionFrame === null) selectionFrame = requestAnimationFrame(paintGridSelection);
}
function scheduleChartDraw() {
  if (chartOverlayFrame !== null) { cancelAnimationFrame(chartOverlayFrame); chartOverlayFrame = null; }
  if (chartDrawFrame === null) chartDrawFrame = requestAnimationFrame(drawChart);
}
function scheduleChartOverlay() {
  if (chartDrawFrame !== null) return;
  if (chartOverlayFrame === null) chartOverlayFrame = requestAnimationFrame(drawChartCursorOverlay);
}
const integerTypes = new Set(["int", "integer", "long", "uchar", "short", "uint", "ulong", "datetime"]);
const floatTypes = new Set(["float", "double", "number", "decimal"]);
const boolTypes = new Set(["bool", "boolean"]);
const objectTypes = new Set(["list", "array", "dict", "object", "json"]);
const i18n = window.rtdbI18n || { t: (k) => k, getLang: () => "zh-CN", setLang: () => {}, onChange: () => {}, translateText: (s) => s };
const t = (key, params) => i18n.t(key, params);
const themeStorageKey = "rtdb.ui.theme";
const uiThemeAliases = {
  light: "minimalist-light",
  dark: "precision-dark",
  "sci-fi": "sci-fi-cyan",
  "neo-brutalism": "minimalist-light"
};
const uiThemes = new Set([
  "minimalist-light",
  "precision-dark",
  "bento-grid",
  "glassmorphism",
  "editorial",
  "sci-fi-cyan",
  "sci-fi-purple",
  "sci-fi-matrix"
]);
const darkThemes = new Set([
  "precision-dark",
  "glassmorphism",
  "sci-fi-cyan",
  "sci-fi-purple",
  "sci-fi-matrix"
]);
// 各视觉风格专属色盲友好 6 色工程序列
const chartPalettes = {
  "minimalist-light": ["#2563EB", "#D97706", "#0D9488", "#DC2626", "#7C3AED", "#64748B"],
  "precision-dark": ["#38BDF8", "#FBBF24", "#34D399", "#FB7185", "#A78BFA", "#94A3B8"],
  "bento-grid": ["#0071E3", "#F59E0B", "#10B981", "#EF4444", "#8B5CF6", "#6E6E73"],
  "glassmorphism": ["#60A5FA", "#FBBF24", "#34D399", "#F472B6", "#A78BFA", "#94A3B8"],
  "editorial": ["#9F1239", "#1D4ED8", "#0F766E", "#A16207", "#6D28D9", "#52525B"],
  "sci-fi-cyan": ["#00E5FF", "#00E6A8", "#FFB020", "#FF4D6D", "#A855F7", "#7DA2C4"],
  "sci-fi-purple": ["#C084FC", "#2DD4BF", "#FBBF24", "#FB7185", "#60A5FA", "#A695C9"],
  "sci-fi-matrix": ["#00E676", "#38BDF8", "#FBBF24", "#FF5C77", "#A78BFA", "#7EB396"]
};
function normalizeTheme(value) {
  const mapped = uiThemeAliases[value] || value;
  return uiThemes.has(mapped) ? mapped : "minimalist-light";
}
function currentTheme() { return normalizeTheme(document.documentElement.dataset.theme); }
function isDarkTheme() { return darkThemes.has(currentTheme()); }
function currentChartPalette() { return chartPalettes[currentTheme()] || chartPalettes["minimalist-light"]; }
function chartThemeTokens() {
  const theme = currentTheme();
  if (theme === "sci-fi-cyan") {
    return {
      canvasBg: "#081121", gridLine: "rgba(0, 229, 255, 0.14)", axisLine: "rgba(0, 229, 255, 0.34)",
      axisText: "#7DA2C4", titleColor: "#E6F7FF", cursorLine: "#00E5FF",
      tooltipBg: "rgba(4, 8, 18, 0.94)", tooltipBorder: "rgba(0, 229, 255, 0.42)",
      tooltipTitle: "#00E5FF", tooltipLabel: "#7DA2C4", tooltipValue: "#E6F7FF"
    };
  }
  if (theme === "sci-fi-purple") {
    return {
      canvasBg: "#120C24", gridLine: "rgba(192, 132, 252, 0.16)", axisLine: "rgba(192, 132, 252, 0.36)",
      axisText: "#A695C9", titleColor: "#F3EEFF", cursorLine: "#C084FC",
      tooltipBg: "rgba(9, 6, 20, 0.95)", tooltipBorder: "rgba(192, 132, 252, 0.44)",
      tooltipTitle: "#C084FC", tooltipLabel: "#A695C9", tooltipValue: "#F3EEFF"
    };
  }
  if (theme === "sci-fi-matrix") {
    return {
      canvasBg: "#081810", gridLine: "rgba(0, 230, 118, 0.14)", axisLine: "rgba(0, 230, 118, 0.34)",
      axisText: "#7EB396", titleColor: "#E2FBE9", cursorLine: "#00E676",
      tooltipBg: "rgba(4, 13, 8, 0.94)", tooltipBorder: "rgba(0, 230, 118, 0.42)",
      tooltipTitle: "#00E676", tooltipLabel: "#7EB396", tooltipValue: "#E2FBE9"
    };
  }
  if (theme === "glassmorphism") {
    return {
      canvasBg: "#0F172A", gridLine: "rgba(255, 255, 255, 0.10)", axisLine: "rgba(255, 255, 255, 0.24)",
      axisText: "#94A3B8", titleColor: "#F8FAFC", cursorLine: "#60A5FA",
      tooltipBg: "rgba(15, 23, 42, 0.95)", tooltipBorder: "rgba(96, 165, 250, 0.38)",
      tooltipTitle: "#F8FAFC", tooltipLabel: "#CBD5E1", tooltipValue: "#F8FAFC"
    };
  }
  if (theme === "precision-dark") {
    return {
      canvasBg: "#111827", gridLine: "rgba(255, 255, 255, 0.08)", axisLine: "rgba(255, 255, 255, 0.20)",
      axisText: "#94A3B8", titleColor: "#F8FAFC", cursorLine: "#94A3B8",
      tooltipBg: "rgba(15, 23, 42, 0.94)", tooltipBorder: "rgba(148, 163, 184, 0.32)",
      tooltipTitle: "#F8FAFC", tooltipLabel: "#CBD5E1", tooltipValue: "#F8FAFC"
    };
  }
  if (theme === "editorial") {
    return {
      canvasBg: "#FAF9F6", gridLine: "#E4E4E7", axisLine: "#C9C7BE",
      axisText: "#52525B", titleColor: "#18181B", cursorLine: "#9F1239",
      tooltipBg: "rgba(255, 255, 255, 0.97)", tooltipBorder: "#C9C7BE",
      tooltipTitle: "#18181B", tooltipLabel: "#52525B", tooltipValue: "#18181B"
    };
  }
  return {
    canvasBg: "#FFFFFF", gridLine: "#E2E8F0", axisLine: "#CBD5E1",
    axisText: "#64748B", titleColor: "#0F172A", cursorLine: "#475569",
    tooltipBg: "rgba(255, 255, 255, 0.96)", tooltipBorder: "#CBD5E1",
    tooltipTitle: "#0F172A", tooltipLabel: "#475569", tooltipValue: "#0F172A"
  };
}
function applyTheme(theme, persist = true) {
  const mode = normalizeTheme(theme);
  document.documentElement.dataset.theme = mode;
  const select = el("theme-select");
  if (select && select.value !== mode) select.value = mode;
  if (persist) { try { localStorage.setItem(themeStorageKey, mode); } catch { /* 主题持久化失败不影响使用 */ } }
  chartStaticKey = null;
  legendSignature = null;
  if (typeof drawChart === "function" && el("tab-chart")) requestAnimationFrame(drawChart);
}
try {
  applyTheme(localStorage.getItem(themeStorageKey) || "minimalist-light", false);
} catch { applyTheme("minimalist-light", false); }
const chartSettingsStorageKey = "rtdb.chart.settings";
const chartTitleDefaults = { color: "#0F172A", fontSize: 13, fontFamily: "Segoe UI", fontStyle: "bold", position: "top-center", gap: 6 };
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
    // 限制持久化样式数量上限（最多保留最近 64 项），防止长期使用撑爆 localStorage。
    while (state.chartStyles.size > 64) {
      const oldest = state.chartStyles.keys().next().value;
      state.chartStyles.delete(oldest);
    }
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
    if (typeof syncLayoutToggleButtons === "function") syncLayoutToggleButtons();
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
function syncPresetControl(selectEl, inputEl, value) {
  if (!selectEl || !inputEl) return;
  const text = String(value ?? "");
  const matched = [...selectEl.options].some((option) => option.value !== "custom" && option.value === text);
  if (matched) {
    selectEl.value = text;
    inputEl.value = text;
    inputEl.hidden = true;
  } else {
    selectEl.value = "custom";
    inputEl.value = text;
    inputEl.hidden = false;
  }
}
function bindPresetControl(selectEl, inputEl, onChange = null) {
  if (!selectEl || !inputEl) return;
  selectEl.onchange = () => {
    if (selectEl.value === "custom") {
      inputEl.hidden = false;
      inputEl.focus({ preventScroll: true });
      if (inputEl.select) inputEl.select();
    } else {
      inputEl.hidden = true;
      inputEl.value = selectEl.value;
    }
    if (onChange) onChange();
  };
  inputEl.oninput = () => { if (onChange) onChange(); };
}
function setSelectValueWithFallback(selectEl, value, suffix = "") {
  if (!selectEl) return;
  selectEl.querySelectorAll('option[data-fallback="true"]').forEach((option) => option.remove());
  const text = String(value);
  if (![...selectEl.options].some((option) => option.value === text)) {
    const option = new Option(suffix ? `${text}${suffix}` : text, text);
    option.dataset.fallback = "true";
    selectEl.append(option);
  }
  selectEl.value = text;
}
function renderLogs() {
  const target = el("logs"); if (!target) return;
  target.replaceChildren();
  if (state.auditLoaded) {
    if (!state.auditRecords.length) { target.append(node("div", "没有符合条件的日志。", "empty")); return; }
    const catLabels = { structure: "结构", data: "数据", system: "系统" };
    for (const record of state.auditRecords) {
      const ok = record.status !== "failure";
      const line = node("div", null, `log-line is-structured ${ok ? "success" : "failure"}`);
      const timeStr = record.timestamp ? new Date(record.timestamp).toLocaleTimeString() : "--:--:--";
      const timeEl = node("span", timeStr, "log-time");
      timeEl.title = record.timestamp ? new Date(record.timestamp).toLocaleString() : "";
      const pill = node("span", ok ? "成功" : "失败", "log-pill");
      const cat = node("span", catLabels[record.category] || record.category || "操作", "log-cat");
      const details = [record.operation || "操作"];
      if (record.table) details.push(record.table);
      if (record.error) details.push(record.error.message || JSON.stringify(record.error));
      else if (record.result && Object.keys(record.result).length) {
        const brief = JSON.stringify(record.result, null, 0);
        details.push(brief.length > 140 ? `${brief.slice(0, 140)}…` : brief);
      }
      const msg = node("span", details.join(" · "), "log-msg");
      const dur = node("span", record.duration_ms != null ? `${Number(record.duration_ms).toFixed(1)} ms` : "", "log-dur");
      line.title = formatAuditLog(record);
      line.append(timeEl, pill, cat, msg, dur);
      target.append(line);
    }
    return;
  }
  if (state.logs.length) for (const message of state.logs) target.append(node("div", message, "log-line"));
  else target.append(node("div", "没有符合条件的日志。", "empty"));
}
function log(message, error = false) {
  state.logs.unshift(`${new Date().toLocaleString()} [${error ? "失败" : "操作"}] ${message}`);
  state.logs.length = Math.min(state.logs.length, 500);
  if (!state.auditLoaded) renderLogs();
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
  const sequence = (state.auditLoadSequence = (state.auditLoadSequence || 0) + 1);
  const query = state.logQuery;
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.pageSize) });
  for (const [key, value] of Object.entries({ date: query.date, keyword: query.keyword, category: query.category, status: query.status })) {
    if (value) params.set(key, value);
  }
  const payload = await request(`logs?${params}`);
  if (sequence !== state.auditLoadSequence) return;
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
  if (state.logConfig) syncPresetControl(el("log-retention-preset"), el("log-retention-days"), state.logConfig.retention_days ?? 0);
}
async function loadLogConfig() {
  const config = await request("log-config");
  state.logConfig = config;
  if (Array.isArray(config.dates)) state.logDates = config.dates;
  renderLogDates(); renderLogSummary();
}
function resetLogPage() { state.logQuery.page = 1; }
function notice(message, error = false) {
  clearTimeout(noticeTimer);
  const target = el("operation-notice");
  target.classList.toggle("is-error", error); target.hidden = !message;
  target.setAttribute("role", error ? "alert" : "status");
  el("operation-message").textContent = message || "";
  if (message && !error) noticeTimer = setTimeout(() => { target.hidden = true; }, 4500);
}
async function run(work, errorTarget = null) {
  if (state.busy) { notice("正在处理，请稍候再操作"); return; }
  state.busy = true; document.body.dataset.busy = "true";
  el("data-grid").setAttribute("aria-busy", "true");
  const editingControls = [...el("data-grid").querySelectorAll(".cell-editor:not(:disabled)")];
  const auditSequence=state.auditLoadSequence||0, writeSeqBefore=state.writeSeq||0;let failed=false;
  editingControls.forEach((control) => { control.disabled = true; });
  if (errorTarget) el(errorTarget).textContent = "";
  try { await work(); }
  catch (error) { failed=true;log(error.message, true); if (errorTarget) el(errorTarget).textContent = error.message; else notice(error.message, true); }
  finally {
    state.busy = false; document.body.dataset.busy = "false";
    el("data-grid").setAttribute("aria-busy", "false");
    editingControls.forEach((control) => { if (control.isConnected) control.disabled = false; });
    // 仅在真正发生写/结构/维护变更且「日志」Tab 处于激活态时异步刷新日志，消除只读/自动刷新冗余轮询。
    if (!failed && !backgroundTasks.pending() && (state.writeSeq || 0) !== writeSeqBefore && (state.auditLoadSequence || 0) === auditSequence && el("tab-logs")?.classList.contains("active")) {
      void loadAuditLogs().catch(() => { /* Keep the last visible log when the log endpoint is unavailable. */ });
    }
  }
}
function isMutatingRequest(path, method, body) {
  if (method === "GET" || method === "HEAD") return false;
  const route = path.split("?")[0];
  if (method === "POST") {
    if (/^tables\/[^/]+\/(query-plan|validate-edits|resolve-records|schema-preview)$/.test(route)) return false;
    if (route === "database-files/select") return false;
    if (/^tables\/[^/]+\/sql$/.test(route)) {
      if (body?.readonly_only) return false;
      if (/^\s*(SELECT|WITH|EXPLAIN|PRAGMA)\b/i.test(body?.sql || "")) return false;
    }
  }
  return true;
}
async function request(path, method = "GET", body, timing = null, { allowBackground = true } = {}) {
  const scoped = /^(tables(?:\/|$)|backup$|restore|definition-|receipt-maintenance|database-snapshots)/.test(path);
  const explicitDatabase = path.startsWith('database-snapshots') ? new URLSearchParams(path.split('?')[1]||'').get('database') : null;
  const requestDatabase = explicitDatabase || state.database;
  if (scoped && !requestDatabase) throw new Error("请先选择或新建数据库");
  const suffix = scoped && requestDatabase && !explicitDatabase ? `${path.includes("?") ? "&" : "?"}database=${encodeURIComponent(requestDatabase)}` : "";
  let endpoint=path+suffix;
  let taskSubmission=null;
  if(allowBackground && backgroundTasks.supports(path,method,body)) {
    if(backgroundTasks.pending())throw new Error('上次后台任务状态尚未确认，请先刷新页面恢复查询，勿重复提交');
    taskSubmission=await backgroundTasks.prepare(path,scoped?requestDatabase:null);
    endpoint+=`${endpoint.includes('?')?'&':'?'}background=1&request_id=${taskSubmission.id}`;
  }
  const requestedAt = performance.now();
  let response;
  try { response = await workspaceFetch(`/api/manage/${endpoint}`, { method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body) }); }
  catch(error) { if(taskSubmission)throw Error('提交响应未确认，已保留请求编号。请刷新恢复任务查询，勿重复提交。');throw error; }
  const text = await response.text(), receivedAt = performance.now();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = null; }
  }
  if (payload === null) {
    if (taskSubmission && response.status !== 202) backgroundTasks.clear();
    const error = new Error(response.ok ? "服务端返回了非 JSON 响应" : `请求失败 ${response.status}`);
    error.status = response.status;
    throw error;
  }
  if (timing) {
    timing.request_ms += receivedAt-requestedAt; timing.parse_ms += performance.now()-receivedAt;
    const server = response.headers.get("Server-Timing")?.match(/app;dur=([\d.]+)/);
    timing.server_ms = server ? Number(server[1]) : null;
    const encoding=response.headers.get('Server-Timing')?.match(/encode;dur=([\d.]+)/);
    timing.encode_ms=encoding?Number(encoding[1]):null;
    Object.assign(timing,payload.timings||{});timing.pagination=payload.pagination_mode||null;
    for(const key of ['queue','execute','open','validate','write','receipt','transaction']){
      const value=response.headers.get('Server-Timing')?.match(new RegExp('(?:^|, )'+key+';dur=([\\d.]+)'));
      if(value)timing[key+'_ms']=Number(value[1]);
    }
  }
  if(taskSubmission && response.status!==202){backgroundTasks.clear();if(response.ok&&taskSubmission.draftKey)await localDrafts.dropKey(taskSubmission.draftKey,taskSubmission.draftRevision);}
  reportAuditWarning(payload);
  if (!response.ok) { const error = new Error(payload.error || `请求失败 ${response.status}`); error.details = payload.details; error.status = response.status; throw error; }
  const mutating = isMutatingRequest(path, method, body);
  if(response.status===202&&payload.task){
    const started=performance.now(),result=await backgroundTasks.follow({...payload.task,draftKey:taskSubmission?.draftKey,draftRevision:taskSubmission?.draftRevision});
    if(timing){timing.request_ms+=performance.now()-started;Object.assign(timing,backgroundTasks.lastTimings||{});}
    if (mutating) state.writeSeq = (state.writeSeq || 0) + 1;
    return result;
  }
  if (mutating) state.writeSeq = (state.writeSeq || 0) + 1;
  return payload;
}
const tablePath = (actionName, name = state.table) => `tables/${encodeURIComponent(name)}/${actionName}`;
function reportAuditWarning(result){if(result?.export_warning){const target=el('definition-warning');target.hidden=false;target.textContent='定义导出待恢复';target.title=result.export_warning;target.onclick=()=>notice(result.export_warning,true);}if(result?.audit_pending)auditDeliveryUI.watch();if(result?.audit_warning){const target=el('audit-warning');target.hidden=false;target.textContent='日志写入待处理';target.title=result.audit_warning;target.onclick=()=>notice(result.audit_warning,true);}}
function requireTable() { if (!state.table) throw new Error("请先选择或保存一张表"); }
function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}
function openDatabaseRepair(file, directory = workspaceTree.currentPath) {
  state.databaseRepairFile = { ...file, directory };
  el("database-repair-file").textContent = `${workspaceDatabasePath(directory, file.filename)} · ${file.status || "未知状态"}`;
  el("database-repair-status").textContent = file.error || "请选择一种处理方式。";
  el("database-repair-error").textContent = "";
  el("workspace-picker").hidePopover();
  el("database-repair-dialog").showModal();
}
async function repairDatabase(actionName) {
  const file = state.databaseRepairFile;
  if (!file) throw new Error("未选择待处理的数据库文件");
  const sameDirectory = file.directory === workspaceTree.currentPath;
  const path = workspaceDatabasePath(file.directory, file.filename);
  if (actionName === "delete-orphan" && !await confirmAction("删除未定义数据表", `将删除 ${path}。文件会先归档，确认后继续。`, file.filename)) return;
  // Background polling follows the active workspace in this tab.
  const result = await request(workspaceEndpoint(`database-files/${actionName}`, file.directory), "POST", {
    filename: file.filename, ...(actionName === "delete-orphan" ? { confirm: file.filename } : {})
  }, null, { allowBackground: sameDirectory });
  el("database-repair-dialog").close(); state.databaseRepairFile = null;
  if (sameDirectory) await refreshTables();
  await loadWorkspaceDirectories();
  await refreshWorkspaceFiles(file.directory);
  const message = actionName === "delete-orphan" ? `已删除未定义数据表：${path}（已归档）`
    : actionName === "rebuild-embedded-definition" ? `已重新生成内置定义表：${path}` : `已重建数据表：${path}`;
  notice(result.warning ? `${message}；${result.warning}` : message); log(message);
}
async function selectDatabaseFile(filename, directory = workspaceTree.currentPath) {
  const changingDirectory = directory !== workspaceTree.currentPath;
  if (changingDirectory) canSwitchWorkspace();
  if (!await discardSchema() || !await discardCellEdits()) return;
  const result = await request(workspaceEndpoint("database-files/select", directory), "POST", { filename });
  if (changingDirectory) {
    navigateWorkspace(directory, { database: result.filename || filename });
    return;
  }
  el("database-file-dialog").close();
  const names = Array.isArray(result.file?.tables) ? result.file.tables.map((item) => item.name) : [];
  const target = names.includes(state.table) ? state.table : names[0] || result.name;
  await activateDatabase(result.filename || filename, target);
  notice(`已选择服务端数据库文件：${filename}${names.length > 1 ? `，包含 ${names.length} 张表` : ""}`);
}
async function activateDatabase(filename, preferredTable) {
  state.database = filename || null;
  state.checkedTables.clear(); state.copiedRows = null; resetSelection();
  await refreshTables();
  const available = state.tables.filter((item) => !item.orphan && item.selectable !== false);
  const target = available.find((item) => item.name === preferredTable) || available[0];
  if (target) await selectTable(target.name);
  if (state.database || !filename) rememberWorkspaceSelection(state.database);
}
function setDatabaseFileTab(name) {
  document.querySelectorAll("[data-database-file-tab]").forEach((button) => {
    const active = button.dataset.databaseFileTab === name;
    button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active));
    const panel = el(button.getAttribute("aria-controls")); panel.hidden = !active;
  });
  if (name === "upload") {
    el("workspace-picker").hidePopover();
    el("database-upload-directory").textContent = `上传目标目录：${workspaceLabel(workspaceTree.currentPath)}`;
  }
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
  const listing = await request("database-files");
  const existing = listing.files.some((item) => item.filename === database.name);
  if (existing && !await confirmAction("覆盖服务端数据库文件", `将覆盖服务端 ${database.name}，同时更新同名定义文件。`, name)) return;
  const encoded = [];
  for (const file of files) encoded.push({ name: file.name, content: await fileToBase64(file) });
  const result = await request("database-files/upload", "POST", { files: encoded, ...(existing ? { confirm: name } : {}) });
  el("database-file-dialog").close(); el("database-file-upload-input").value = ""; renderDatabaseUploadSelection();
  state.database = result.filename; state.checkedTables.clear(); resetSelection();
  await refreshTables();
  const target = result.tables?.[0]?.name || result.name;
  if (target && state.tables.some((item) => item.name === target)) await selectTable(target);
  if (state.database) rememberWorkspaceSelection(state.database);
  const message = `${result.replaced ? "已覆盖并更新" : "已上传"} ${result.filename}，${result.tables?.length ?? 1} 张表，${Number(result.row_count).toLocaleString()} 条记录`;
  notice(message); log(message);
}
async function createDatabaseFile() {
  const directory = workspaceTree.browsingPath;
  const changingDirectory = directory !== workspaceTree.currentPath;
  if (changingDirectory) canSwitchWorkspace();
  if (!await discardSchema() || !await discardCellEdits()) return;
  const name = el("database-file-new-name").value.trim(), label = el("database-file-new-label").value.trim();
  if (!name) throw new Error("数据库英文名不能为空");
  const firstTable = el("database-file-first-table").value.trim();
  const result = await request(workspaceEndpoint("database-files/create", directory), "POST", { name, chn_name: label || name, tables: firstTable ? [{
    head: { eng_name: firstTable, chn_name: firstTable }, columns: [
      { eng_name: "id", chn_name: "编号", data_type: "int", primary_key: true, disp_type: "整数", disp_menu: "" },
      { eng_name: "name", chn_name: "名称", data_type: "str", primary_key: false, disp_type: "文本", disp_menu: "" }
    ] }] : [] });
  el("database-file-new-name").value = ""; el("database-file-new-label").value = ""; el("database-file-first-table").value = "";
  if (changingDirectory) {
    navigateWorkspace(directory, { database: result.filename });
    return;
  }
  state.database = result.filename; state.checkedTables.clear(); resetSelection();
  await refreshTables(); if (firstTable) await selectTable(firstTable); else tab("data"); el("database-file-dialog").close();
  if (state.database) rememberWorkspaceSelection(state.database);
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
  // 需名称确认时聚焦输入框，纯高危确认默认聚焦取消按钮防误按回车。
  if (expected) el("confirm-input").focus({ preventScroll: true });
  else el("confirm-cancel")?.focus({ preventScroll: true });
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
    if (isIntBoolDisplayColumn(column) && value !== 0n && value !== 1n) {
      throw new Error(`${column.eng_name} 布尔类型只能取值 0 或 1`);
    }
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
  localDrafts.schedule();
  const count = state.pendingEdits.size, drafts = draftRecords().length;
  const hasAny = Boolean(count || drafts);
  if (el("preview-cell-edits")) el("preview-cell-edits").hidden = !hasAny;
  el("save-cell-edits").hidden = !hasAny;
  el("discard-cell-edits").hidden = !hasAny;
  el("save-cell-edits").textContent = drafts ? `保存修改（新增 ${drafts} 条）` : `保存改单元格（${count}）`;
  el("chart-edit-hint").textContent = [drafts ? `${drafts} 条新增记录待保存` : "", count ? `${count} 个单元格已修改` : ""].filter(Boolean).join(" · ") || "未选择修改";
  updateRecordsToolbar();
  updateAutoRefreshStatus();
}
function setPendingCell(record, column, raw, updateActions = true) {
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
  if (updateActions) updateEditActions();
}
const BOOL_DISPLAY_TYPE = "布尔";
const TIME_DISPLAY_TYPE = "时间类型";
const MENU_DISPLAY_TYPE = "菜单类型";
const CHOICE_DISPLAY_TYPE = "选择类型";
const DEFAULT_TIME_FORMAT = "年-月-日 时:分:秒";
const TIME_FORMAT_PRESETS = [
  "年-月-日 时:分:秒",
  "年-月-日 时:分",
  "年-月-日",
  "月-日 时:分:秒",
  "时:分:秒",
  "时:分",
  "年-月-日 时:分:秒.毫秒",
  "YYYY-MM-DD HH:mm:ss"
];
const VALUE_DISPLAY_MODE_KEY = "rtdb.ui.valueDisplayMode";
function currentValueDisplayMode() {
  return el("value-display-mode")?.value === "raw" ? "raw" : "display";
}
function applyValueDisplayMode(mode, persist = true) {
  const normalized = mode === "raw" ? "raw" : "display";
  const select = el("value-display-mode");
  if (select && select.value !== normalized) select.value = normalized;
  if (persist) { try { localStorage.setItem(VALUE_DISPLAY_MODE_KEY, normalized); } catch { /* 忽略本地存储异常 */ } }
}
function isIntegerSchemaType(kind) {
  const type = String(kind || "str").toLowerCase();
  return integerTypes.has(type) || ["uchar", "short", "uint", "ulong", "datetime"].includes(type);
}
function isStringSchemaType(kind) {
  const type = String(kind || "str").toLowerCase();
  return ["str", "string", "text", "char", "varchar"].includes(type);
}
function isChoiceSchemaType(kind) {
  return isIntegerSchemaType(kind) || isStringSchemaType(kind);
}
function isTimeDisplayColumn(column) {
  if (!column || !isIntegerSchemaType(column.data_type)) return false;
  const disp = String(column.disp_type || "").trim();
  const lower = disp.toLowerCase();
  return disp === TIME_DISPLAY_TYPE || disp === "时间" || lower === "time" || lower === "timestamp";
}
function isMenuDisplayColumn(column) {
  if (!column || !isIntegerSchemaType(column.data_type)) return false;
  const disp = String(column.disp_type || "").trim();
  const lower = disp.toLowerCase();
  return disp === MENU_DISPLAY_TYPE || disp === "菜单" || lower === "menu_dict" || lower === "enum_dict";
}
function isIntBoolDisplayColumn(column) {
  if (!column || !isIntegerSchemaType(column.data_type)) return false;
  const disp = String(column.disp_type || "").trim();
  const lower = disp.toLowerCase();
  return disp === BOOL_DISPLAY_TYPE || lower === "bool" || lower === "boolean";
}
function isChoiceDisplayColumn(column) {
  if (!column || !isChoiceSchemaType(column.data_type)) return false;
  const disp = String(column.disp_type || "").trim();
  const lower = disp.toLowerCase();
  return disp === CHOICE_DISPLAY_TYPE || disp === "选择" || lower === "choice_set" || lower === "set";
}
/** 无损解析整数 JSON 数组词元，防止 64 位大整数经 JSON.parse 舍入丢失精度。 */
function parseIntegerJsonArrayTokens(text) {
  const trimmed = String(text ?? "").trim();
  if (!trimmed.startsWith("[") || !trimmed.endsWith("]")) return null;
  const inner = trimmed.slice(1, -1).trim();
  if (!inner) return [];
  const rawParts = inner.split(",");
  const tokens = [];
  for (const part of rawParts) {
    const item = part.trim();
    if (/^-?(?:0|[1-9]\d*)$/.test(item)) {
      tokens.push(item);
    } else if (/^"(?:-?(?:0|[1-9]\d*))"$/.test(item)) {
      tokens.push(item.slice(1, -1));
    } else {
      return null;
    }
  }
  return tokens;
}
function getChoiceSetCache(raw, dataType = "str") {
  const text = String(raw ?? "").trim();
  if (!text) return { list: [], set: new Set() };
  const isInt = isIntegerSchemaType(dataType);
  const cacheKey = `${isInt ? "I" : "S"}:${text}`;
  const cached = choiceSetCache.get(cacheKey);
  if (cached) return cached;
  const seen = new Set();
  const result = [];
  if (isInt) {
    const exactTokens = parseIntegerJsonArrayTokens(text);
    const sourceList = exactTokens ?? (() => {
      try {
        const parsed = JSON.parse(text);
        return Array.isArray(parsed) ? parsed : [];
      } catch { return []; }
    })();
    for (const item of sourceList) {
      if (typeof item === "boolean" || item == null) continue;
      const s = typeof item === "number"
        ? (Number.isInteger(item) ? String(item) : "")
        : typeof item === "string" ? item.trim() : "";
      if (!/^-?\d+$/.test(s)) continue;
      try {
        const big = BigInt(s);
        if (big < -(2n ** 63n) || big >= 2n ** 63n) continue;
        const norm = big.toString();
        if (seen.has(norm)) continue;
        seen.add(norm);
        result.push(norm);
      } catch { continue; }
    }
  } else {
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) {
        for (const item of parsed) {
          if (typeof item !== "string" && typeof item !== "number") continue;
          const s = String(item);
          if (!s.trim() || seen.has(s)) continue;
          seen.add(s);
          result.push(s);
        }
      }
    } catch { /* 非法 JSON 返回空集合 */ }
  }
  const entry = { list: result, set: seen };
  if (choiceSetCache.size >= 256) choiceSetCache.clear();
  choiceSetCache.set(cacheKey, entry);
  return entry;
}
function parseChoiceSet(raw, dataType = "str") {
  return getChoiceSetCache(raw, dataType).list;
}
function serializeChoiceSet(values, dataType = "str") {
  if (!Array.isArray(values) || !values.length) return "";
  if (isIntegerSchemaType(dataType)) {
    const seen = new Set();
    const parts = [];
    for (const raw of values) {
      const s = String(raw ?? "").trim();
      if (!/^-?\d+$/.test(s)) continue;
      const norm = BigInt(s).toString();
      if (seen.has(norm)) continue;
      seen.add(norm);
      parts.push(norm);
    }
    return parts.length ? `[${parts.join(",")}]` : "";
  }
  const seen = new Set();
  const list = [];
  for (const raw of values) {
    const s = String(raw ?? "");
    if (!s.trim() || seen.has(s)) continue;
    seen.add(s);
    list.push(s);
  }
  return list.length ? JSON.stringify(list) : "";
}
function getMenuDictCache(raw) {
  const text = String(raw ?? "").trim();
  if (!text) return { entries: [], map: new Map() };
  const cached = menuDictCache.get(text);
  if (cached) return cached;
  const entries = [];
  const map = new Map();
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      for (const [key, value] of Object.entries(parsed)) {
        const k = String(key).trim();
        if (!k) continue;
        const v = value == null ? "" : String(value);
        entries.push({ key: k, value: v });
        map.set(k, v);
      }
    }
  } catch { /* 非法 JSON 返回空菜单 */ }
  const entry = { entries, map };
  if (menuDictCache.size >= 256) menuDictCache.clear();
  menuDictCache.set(text, entry);
  return entry;
}
function parseMenuDict(raw) {
  return getMenuDictCache(raw).entries;
}
function serializeMenuEntries(entries) {
  const dict = {};
  for (const item of entries) {
    dict[String(item.key).trim()] = String(item.value ?? "");
  }
  return Object.keys(dict).length ? JSON.stringify(dict) : "";
}
function formatMenuDictValue(column, rawVal) {
  if (rawVal == null || rawVal === "") return null;
  const { map } = getMenuDictCache(column?.disp_menu);
  if (!map.size) return null;
  const lookup = String(rawVal).trim();
  return map.has(lookup) ? map.get(lookup) : null;
}
function timestampToDate(value) {
  if (value == null || value === "") return null;
  let ms;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    const abs = Math.abs(value);
    ms = abs >= 1e17 ? value / 1e6 : abs >= 1e14 ? value / 1e3 : abs >= 1e11 ? value : value * 1000;
  } else {
    const text = String(value).trim();
    if (!/^-?\d+$/.test(text)) return null;
    try {
      const big = BigInt(text);
      const abs = big < 0n ? -big : big;
      if (abs >= 100000000000000000n) ms = Number(big / 1000000n);
      else if (abs >= 100000000000000n) ms = Number(big / 1000n);
      else if (abs >= 100000000000n) ms = Number(big);
      else ms = Number(big) * 1000;
    } catch { return null; }
  }
  if (!Number.isFinite(ms)) return null;
  const date = new Date(ms);
  return Number.isNaN(date.getTime()) ? null : date;
}
function formatTimestampValue(value, format = DEFAULT_TIME_FORMAT) {
  const date = timestampToDate(value);
  if (!date) return null;
  const pad = (num, len = 2) => String(num).padStart(len, "0");
  const tokens = {
    YYYY: String(date.getFullYear()),
    YY: String(date.getFullYear()).slice(-2),
    MM: pad(date.getMonth() + 1),
    DD: pad(date.getDate()),
    HH: pad(date.getHours()),
    mm: pad(date.getMinutes()),
    ss: pad(date.getSeconds()),
    SSS: pad(date.getMilliseconds(), 3),
    "年": String(date.getFullYear()),
    "月": pad(date.getMonth() + 1),
    "日": pad(date.getDate()),
    "时": pad(date.getHours()),
    "分": pad(date.getMinutes()),
    "秒": pad(date.getSeconds()),
    "毫秒": pad(date.getMilliseconds(), 3)
  };
  const pattern = String(format || "").trim() || DEFAULT_TIME_FORMAT;
  return pattern.replace(/YYYY|YY|MM|DD|HH|mm|ss|SSS|毫秒|年|月|日|时|分|秒/g, (token) => tokens[token] ?? token);
}
function detectTimestampUnitFromValue(value) {
  if (value == null || value === "") return null;
  const text = String(value).trim();
  if (!/^-?\d+$/.test(text)) return null;
  try {
    const big = BigInt(text);
    const abs = big < 0n ? -big : big;
    if (abs >= 100000000000000000n) return "ns";
    if (abs >= 100000000000000n) return "us";
    if (abs >= 100000000000n) return "ms";
    if (abs >= 100000000n) return "s";
    return null;
  } catch { return null; }
}
function detectTimestampUnit(value, column) {
  const direct = detectTimestampUnitFromValue(value);
  if (direct) return direct;
  if (column && Array.isArray(state.records)) {
    for (const rec of state.records) {
      const unit = detectTimestampUnitFromValue(rec?.row?.[column.eng_name]);
      if (unit) return unit;
    }
  }
  return value != null && String(value).trim() !== "" ? "s" : "ms";
}
function dateToDatetimeLocalValue(date) {
  const pad = (num, len = 2) => String(num).padStart(len, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
function extractSubMsOffset(value, unit) {
  if (value == null || value === "" || (unit !== "us" && unit !== "ns")) return 0n;
  const text = String(value).trim();
  if (!/^-?\d+$/.test(text)) return 0n;
  try {
    const big = BigInt(text);
    const scale = unit === "ns" ? 1000000n : 1000n;
    const rem = big % scale;
    return rem < 0n ? rem + scale : rem;
  } catch { return 0n; }
}
function floorDivBigInt(a, b) {
  const q = a / b;
  const r = a % b;
  return r < 0n ? q - 1n : q;
}
function datetimeLocalToTimestamp(localStr, msValue, unit, subMsOffset = 0n) {
  const text = String(localStr || "").trim();
  if (!text) return null;
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/);
  if (!match) return null;
  const [, yr, mo, dy, hr, mn, sc = "0"] = match;
  const msNum = unit === "s" ? 0 : Math.max(0, Math.min(999, Math.trunc(Number(msValue) || 0)));
  const date = new Date(Number(yr), Number(mo) - 1, Number(dy), Number(hr), Number(mn), Number(sc), msNum);
  const epochMs = date.getTime();
  if (!Number.isFinite(epochMs)) return null;
  const bigMs = BigInt(Math.trunc(epochMs));
  if (unit === "s") return floorDivBigInt(bigMs, 1000n).toString();
  if (unit === "us") return (bigMs * 1000n + (subMsOffset >= 0n && subMsOffset < 1000n ? subMsOffset : 0n)).toString();
  if (unit === "ns") return (bigMs * 1000000n + (subMsOffset >= 0n && subMsOffset < 1000000n ? subMsOffset : 0n)).toString();
  return bigMs.toString();
}
function parseIntBoolValue(rawVal) {
  if (rawVal == null || rawVal === "") return { valid: true, value: null };
  const s = String(rawVal).trim();
  if (!/^-?\d+$/.test(s)) return { valid: false, value: null };
  try {
    const big = BigInt(s);
    if (big === 0n) return { valid: true, value: 0 };
    if (big === 1n) return { valid: true, value: 1 };
    return { valid: false, value: null };
  } catch { return { valid: false, value: null }; }
}
function formatGridDisplayValue(column, rawVal, fallbackText) {
  if (rawVal == null || rawVal === "" || currentValueDisplayMode() !== "display") {
    return { text: fallbackText, formatted: false, invalid: false, invalidDetail: "", rawText: fallbackText };
  }
  if (isTimeDisplayColumn(column)) {
    const formatted = formatTimestampValue(rawVal, column.disp_menu || DEFAULT_TIME_FORMAT);
    if (formatted != null) return { text: formatted, formatted: true, invalid: false, invalidDetail: "", rawText: fallbackText };
  } else if (isIntBoolDisplayColumn(column)) {
    const parsed = parseIntBoolValue(rawVal);
    if (parsed.valid) {
      const label = parsed.value === 1 ? "1（是）" : "0（否）";
      return { text: label, formatted: true, invalid: false, invalidDetail: "", rawText: fallbackText };
    }
    const invalidText = t("cell.invalidMenu", { value: fallbackText });
    const invalidDetail = t("cell.invalidBoolTip", { value: fallbackText });
    return { text: invalidText, formatted: false, invalid: true, invalidDetail, rawText: fallbackText };
  } else if (isMenuDisplayColumn(column)) {
    const { map } = getMenuDictCache(column.disp_menu);
    if (map.size) {
      const lookup = String(rawVal).trim();
      if (map.has(lookup)) return { text: map.get(lookup), formatted: true, invalid: false, invalidDetail: "", rawText: fallbackText };
      const invalidText = t("cell.invalidMenu", { value: fallbackText });
      const invalidDetail = t("cell.invalidMenuTip", { value: fallbackText });
      return { text: invalidText, formatted: false, invalid: true, invalidDetail, rawText: fallbackText };
    }
  } else if (isChoiceDisplayColumn(column)) {
    const { set } = getChoiceSetCache(column.disp_menu, column.data_type);
    if (set.size) {
      const isInt = isIntegerSchemaType(column.data_type);
      let lookup = String(rawVal);
      if (isInt && /^-?\d+$/.test(lookup.trim())) {
        try { lookup = BigInt(lookup.trim()).toString(); } catch { /* 保持原文本 */ }
      }
      if (set.has(lookup)) {
        return { text: fallbackText, formatted: false, invalid: false, invalidDetail: "", rawText: fallbackText };
      }
      const invalidText = t("cell.invalidMenu", { value: fallbackText });
      const invalidDetail = t("cell.invalidChoiceTip", { value: fallbackText });
      return { text: invalidText, formatted: false, invalid: true, invalidDetail, rawText: fallbackText };
    }
  }
  return { text: fallbackText, formatted: false, invalid: false, invalidDetail: "", rawText: fallbackText };
}
function isNumericColumn(column) {
  const kind = columnKind(column);
  return integerTypes.has(kind) || floatTypes.has(kind);
}
function renderGridCellValue(record, column, cell) {
  const pending = pendingCell(record, column.eng_name);
  const rawVal = record.row[column.eng_name];
  const isNull = !pending && !record.draftId && rawVal == null;
  const baseText = pending ? pending.raw : record.draftId ? cellText(rawVal) : display(rawVal);
  const formatSource = pending ? pending.raw : rawVal;
  const shown = isNull ? { text: baseText, formatted: false, invalid: false, invalidDetail: "", rawText: baseText } : formatGridDisplayValue(column, formatSource, baseText);
  cell.classList.toggle("is-numeric", isNumericColumn(column));
  const isIntBool = isIntBoolDisplayColumn(column);
  const intBoolParsed = isIntBool ? parseIntBoolValue(isNull ? null : formatSource) : null;
  const renderIntBoolToggle = state.editable && isIntBool && currentValueDisplayMode() === "display" && !isReadOnlyKey(record, column) && intBoolParsed.valid;
  if (state.editable && boolTypes.has(columnKind(column)) && !isReadOnlyKey(record, column)) {
    const input = node("input"); input.type = "checkbox"; input.className = "cell-toggle";
    input.checked = baseText === "true" || baseText === true; input.indeterminate = baseText === "NULL" || baseText === "";
    input.setAttribute("aria-label", `${record.ref?.key.join(" / ") || `新增${record.draftId}`} ${column.eng_name}`);
    input.onchange = () => {
      if (state.busy) { renderGridCellValue(record, column, cell); return; }
      const history = editSnapshot();
      setPendingCell(record, column, String(input.checked)); input.indeterminate = false;
      rememberEdit(history);
      cell.classList.toggle("cell-modified", Boolean(pendingCell(record, column.eng_name)));
      cell.title = pendingCell(record, column.eng_name) ? "已修改，Ctrl+S 保存" : String(input.checked);
      scheduleChartDraw();
    };
    cell.replaceChildren(input);
  } else if (renderIntBoolToggle) {
    const input = node("input"); input.type = "checkbox"; input.className = "cell-toggle";
    input.checked = intBoolParsed.value === 1;
    input.indeterminate = intBoolParsed.value === null;
    input.setAttribute("aria-label", `${record.ref?.key.join(" / ") || `新增${record.draftId}`} ${column.eng_name}`);
    input.onchange = () => {
      if (state.busy) { renderGridCellValue(record, column, cell); return; }
      const history = editSnapshot();
      const nextVal = input.checked ? "1" : "0";
      setPendingCell(record, column, nextVal); input.indeterminate = false;
      rememberEdit(history);
      cell.classList.toggle("cell-modified", Boolean(pendingCell(record, column.eng_name)));
      cell.classList.remove("cell-invalid");
      cell.title = pendingCell(record, column.eng_name) ? `已修改（原值: ${nextVal}），Ctrl+S 保存` : `${nextVal === "1" ? t("bool.intTrue") : t("bool.intFalse")}`;
      scheduleChartDraw();
    };
    input.onkeydown = (event) => {
      if ((event.key === "Delete" || event.key === "Backspace") && !column.primary_key && !state.busy) {
        event.preventDefault(); event.stopPropagation();
        const history = editSnapshot();
        setPendingCell(record, column, "");
        rememberEdit(history);
        renderGridCellValue(record, column, cell);
        cell.querySelector(".cell-toggle")?.focus({ preventScroll: true });
        scheduleChartDraw();
      }
    };
    cell.replaceChildren(input);
  } else {
    const span = node("span", shown.text, isNull ? "cell-value cell-null" : "cell-value");
    cell.replaceChildren(span);
  }
  cell.classList.toggle("cell-modified", Boolean(pending));
  cell.classList.toggle("cell-invalid", Boolean(shown.invalid));
  const displayTip = shown.invalid
    ? shown.invalidDetail
    : shown.formatted ? `${shown.text}（原值: ${shown.rawText}）` : shown.text;
  cell.title = isReadOnlyKey(record, column)
    ? (shown.invalid ? shown.invalidDetail : "KEY 主键列：可选择，不可修改，冻结在左侧")
    : record.draftId
      ? (shown.invalid ? shown.invalidDetail : "待新增记录：双击填写，KEY 必填；保存后写入数据库")
      : pending
        ? `${shown.invalid ? `${shown.invalidDetail} · ` : ""}已修改${shown.formatted ? `（原值: ${shown.rawText}）` : ""}，保存前仅保留在当前页面`
        : displayTip;
}
function syncGridCell(record, field) {
  const cell = gridCells.get(record)?.get(field);
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
function columnMenuEntries(column) {
  if (isTimeDisplayColumn(column)) return [];
  if (isIntBoolDisplayColumn(column)) {
    return [{ value: "0", label: t("bool.intFalse") }, { value: "1", label: t("bool.intTrue") }];
  }
  if (isMenuDisplayColumn(column)) {
    return parseMenuDict(column?.disp_menu).map((item) => ({
      value: item.key,
      label: item.value ? `${item.value} (${item.key})` : item.key
    }));
  }
  if (isChoiceDisplayColumn(column)) {
    return parseChoiceSet(column?.disp_menu, column?.data_type).map((item) => ({ value: item, label: item }));
  }
  return [];
}
function columnMenuOptions(column) {
  return columnMenuEntries(column).map((item) => item.value);
}
function inlineCellInput(record, column, index, cell, initialText = null) {
  const kind = columnKind(column);
  if (isReadOnlyKey(record, column) || objectTypes.has(kind) && !record.draftId) return false;
  if (isTimeDisplayColumn(column) && currentValueDisplayMode() === "display" && initialText === null) {
    openTimeEditDialog(record, column, cell);
    return true;
  }
  if (cell.querySelector(".cell-toggle")) { cell.querySelector(".cell-toggle").focus({ preventScroll: true }); return true; }
  if (cell.querySelector(".cell-editor")) return true;
  const menu = columnMenuEntries(column);
  const useSelect = menu.length > 0 || boolTypes.has(kind);
  const pending = pendingCell(record, column.eng_name), raw = pending?.raw ?? cellText(record.row[column.eng_name]);
  const before = structuredClone(record.row[column.eng_name]), history = editSnapshot();
  const input = node(useSelect ? "select" : "input"); input.className = "cell-editor"; input.dataset.rowIndex = String(index); input.dataset.field = column.eng_name;
  input.setAttribute("aria-label", `${record.draftId ? `新增${record.draftId}` : record.ref.key.join(" / ")} ${column.eng_name}`);
  if (useSelect) {
    if (!column.primary_key) input.append(new Option("（空）", ""));
    if (boolTypes.has(kind)) {
      input.append(new Option("true（是）", "true"), new Option("false（否）", "false"));
    } else {
      for (const item of menu) input.append(new Option(item.label, item.value));
    }
    if (raw !== "" && ![...input.options].some((option) => option.value === raw)) {
      input.append(new Option(`${raw}（当前）`, raw));
    }
    input.value = raw;
  } else {
    input.type = "text"; input.value = initialText !== null ? initialText : raw; input.spellcheck = false;
  }
  const change = () => {
    const next = input.value;
    setPendingCell(record, column, next); input.setCustomValidity("");
    if (!rawMatchesBaseline(column, next, record.baseline?.[column.eng_name])) cell.classList.add("cell-modified"); else cell.classList.remove("cell-modified");
    scheduleChartDraw();
  };
  input.oninput = change; input.onchange = change;
  input.onblur = () => { rememberEdit(history); renderGridCellValue(record, column, cell); };
  input.onkeydown = (event) => {
    if (event.isComposing || !["Enter", "Escape", "Tab"].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    if (event.key === "Escape") {
      input.oninput = input.onchange = null;
      record.row[column.eng_name] = before;
      if (pending) state.pendingEdits.set(cellKey(record, column.eng_name), pending);
      else state.pendingEdits.delete(cellKey(record, column.eng_name));
      updateEditActions(); drawChart();
    }
    input.blur(); cell.focus({ preventScroll: true });
    if (event.key !== "Escape") moveGridFocus(index, Number(cell.dataset.cellColumn), event.key, event.shiftKey, false, false);
  };
  const placeholder = cell.querySelector(".cell-value");
  if (placeholder) placeholder.classList.add("cell-editor-placeholder");
  cell.replaceChildren(...(placeholder ? [placeholder, input] : [input])); cell.classList.toggle("cell-modified", Boolean(pending));
  cell.title = pending ? "已修改，保存前仅保留在当前页面" : raw || "NULL";
  input.focus({ preventScroll: true });
  if (!useSelect) {
    if (initialText !== null) {
      change();
      input.setSelectionRange(initialText.length, initialText.length);
    } else input.select();
  }
  return true;
}
async function discardSchema() {
  if(!state.schemaDirty)return true;
  if(!await confirmAction("离开结构编辑", "表结构有未保存的修改，确认放弃这些修改？"))return false;
  clearTimeout(localDrafts.timer);await localDrafts.dropKey(localDrafts.key('schema'));state.schemaDirty=false;return true;
}
function tab(name) {
  closeSqlHistory();
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === name; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${name}`));
  if (name === "schema" && state.definition && !state.schemaRendered) {
    renderSchema(state.definition);
  }
  if (name === "data") requestAnimationFrame(drawChart);
}
function bottomTab(name) {
  document.querySelectorAll("[data-bottom-tab]").forEach((button) => {
    const active = button.dataset.bottomTab === name;
    button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1;
    const panel = el(button.getAttribute("aria-controls")); panel.classList.toggle("active", active); panel.hidden = !active;
    el(`${button.dataset.bottomTab}-actions`).hidden = !active;
  });
  if (name === "logs") run(async () => { await loadAuditLogs(); });
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
  if (!visible.length) target.append(node("p", !state.database ? "请先选择或新建数据库" : state.tables.length ? "没有匹配的表" : "当前数据库为空，点击“新建表”开始", "empty"));
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
  state.databases = (await request("databases")).databases;
  if (state.database && !state.databases.some((item) => item.filename === state.database)) { state.database = null; resetSelection(); }
  el("database-select").replaceChildren(new Option("选择数据库…", ""));
  for (const item of state.databases) {
    const option = new Option(`${item.label} · ${item.filename}${item.error ? "（异常）" : item.read_only ? "（只读）" : ""}`, item.filename);
    el("database-select").append(option);
  }
  el("database-select").value = state.database || "";
  el("query").disabled = !state.database;
  updateSchemaLocks();
  state.tables = state.database ? (await request("tables")).tables : [];
  state.checkedTables = new Set([...state.checkedTables].filter((name) => state.tables.some((item) => item.name === name)));
  renderTables();
}
function renderCurrentDatabaseFile() {
  const target = el("current-database-file");
  const item = state.tables.find((entry) => entry.name === state.table);
  const filename = state.database || (state.table ? item?.database_file || `${state.table}.sqlite3` : "");
  const directory = state.dataDir === "." ? "" : state.dataDir.replace(/[\\/]+$/, "");
  const separator = state.dataDir.includes("\\") ? "\\" : "/";
  target.textContent = filename || "未选择数据库";
  target.title = filename ? `当前数据库：${directory ? directory + separator : ""}${filename}` : "请先选择或新建数据库";
  target.setAttribute("aria-label", filename ? `当前数据库文件：${filename}` : "未选择数据库");
}
function updateRecordsToolbar() {
  const title = el("records-table-title"), meta = el("records-table-meta");
  if (title) {
    title.textContent = state.table && state.definition ? `${chineseName(state.definition.head)} / ${state.table}` : "未选择表";
  }
  if (meta) {
    meta.textContent = state.table && state.definition
      ? `${state.definition.columns.length} 字段 · 共 ${Number(state.tableTotal || 0).toLocaleString()} 条`
      : "";
  }
  const hasTable = Boolean(state.table), readOnly = isReadOnlyDatabase(), pending = hasPendingData();
  const rowIndexes = selectedRowIndexes(null, null);
  const savedSelectedCount = rowIndexes.filter((idx) => state.records[idx] && !state.records[idx].draftId).length;
  const format = el("export-format")?.value || "csv";
  if (el("btn-add-row")) el("btn-add-row").disabled = !hasTable || readOnly || draftRecords().length >= 500;
  if (el("btn-batch-edit")) el("btn-batch-edit").disabled = !hasTable || !state.editable || readOnly || pending || !savedSelectedCount;
  if (el("btn-import-data")) el("btn-import-data").disabled = !hasTable || readOnly || pending;
  if (el("export-format")) el("export-format").disabled = !hasTable || !state.lastQuery;
  if (el("btn-export-data")) el("btn-export-data").disabled = !hasTable || !state.lastQuery || (format === "csv" && !state.fields.size);
  if (el("btn-delete-rows")) el("btn-delete-rows").disabled = !hasTable || !state.editable || readOnly || !rowIndexes.length;
}
function autoRefreshPauseReason() {
  const seconds = Number(el("auto-refresh")?.value || 0);
  if (!seconds) return "";
  if (state.busy) return "操作执行中";
  if (document.hidden) return "页面处于后台";
  if (document.querySelector("dialog[open]")) return "弹窗打开中";
  if (state.contextRecord) return "右键菜单打开中";
  if (state.schemaDirty) return "表结构有未保存修改";
  if (hasPendingData()) return "存在未保存的单元格或新行";
  if (state.selected.size || state.selectedColumns.size || state.selectedCells.size) return "当前存在选区（Esc 取消）";
  return "";
}
function updateAutoRefreshStatus() {
  const badge = el("auto-refresh-status"); if (!badge) return;
  const reason = autoRefreshPauseReason();
  badge.hidden = !reason;
  if (reason) {
    badge.textContent = "已暂停";
    badge.title = `自动刷新已暂停：${reason}`;
  } else {
    badge.textContent = "";
    badge.title = "";
  }
}
function quickFilterColumns() {
  return state.definition?.columns?.length ? state.definition.columns : state.resultColumns;
}
function syncQuickFilterValueControl() {
  const fieldEl = el("filter-field"), opEl = el("filter-op"), host = el("filter-value-host");
  const applyBtn = el("apply-quick-filter"), resetBtn = el("reset-quick-filter");
  if (!fieldEl || !opEl || !host) return;
  const fieldName = fieldEl.value, op = opEl.value;
  const hasField = Boolean(fieldName);
  const needsValue = hasField && !["IS NULL", "IS NOT NULL"].includes(op);
  opEl.hidden = !hasField;
  host.hidden = !needsValue;
  if (applyBtn) applyBtn.hidden = !hasField;
  const customSql = Boolean(state.table && el("sql-editor").value.trim() && normalizeSqlText(el("sql-editor").value) !== normalizeSqlText(defaultSql()));
  if (resetBtn) resetBtn.hidden = !hasField && !customSql;
  if (!needsValue) return;
  const column = quickFilterColumns().find((item) => item.eng_name === fieldName);
  const kind = columnKind(column), menu = columnMenuEntries(column);
  const previous = el("filter-value")?.value ?? "";
  let control;
  if (boolTypes.has(kind)) {
    control = node("select"); control.id = "filter-value"; control.setAttribute("aria-label", "筛选值");
    control.append(new Option("true（是）", "true"), new Option("false（否）", "false"));
    if (["true", "false"].includes(previous)) control.value = previous;
  } else if (menu.length && op !== "LIKE") {
    control = node("select"); control.id = "filter-value"; control.setAttribute("aria-label", "筛选值");
    control.append(new Option("选择值…", ""));
    for (const item of menu) control.append(new Option(item.label, item.value));
    if (menu.some((item) => item.value === previous)) control.value = previous;
  } else {
    control = node("input"); control.id = "filter-value"; control.type = "text"; control.placeholder = op === "LIKE" ? "包含关键词" : "筛选值";
    control.setAttribute("aria-label", "筛选值"); control.value = previous;
    control.onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); run(applyQuickFilter); } };
  }
  host.replaceChildren(control);
}
function configureQuickFilter() {
  const fieldEl = el("filter-field"); if (!fieldEl) return;
  const previous = fieldEl.value;
  const columns = quickFilterColumns();
  fieldEl.replaceChildren(new Option("快捷筛选…", ""));
  for (const column of columns) {
    fieldEl.append(new Option(`${chineseName(column)} / ${column.eng_name}`, column.eng_name));
  }
  fieldEl.disabled = !state.table || !columns.length;
  fieldEl.value = columns.some((column) => column.eng_name === previous) ? previous : "";
  if (!fieldEl.value) el("filter-op").value = "=";
  syncQuickFilterValueControl();
}
function quoteSqlIdent(name) {
  return `"${String(name).replace(/"/g, '""')}"`;
}
function quoteSqlString(text) {
  return `'${String(text).replace(/'/g, "''")}'`;
}
async function applyQuickFilter() {
  requireTable();
  const fieldName = el("filter-field").value;
  if (!fieldName) throw new Error("请先选择筛选字段");
  const op = el("filter-op").value;
  const column = quickFilterColumns().find((item) => item.eng_name === fieldName);
  const ident = quoteSqlIdent(fieldName), tableIdent = quoteSqlIdent(state.table);
  let clause;
  if (op === "IS NULL" || op === "IS NOT NULL") {
    clause = `${ident} ${op}`;
  } else {
    const raw = el("filter-value")?.value ?? "";
    if (!raw.length) throw new Error("请输入或选择筛选值");
    const kind = columnKind(column);
    if (op === "LIKE") {
      clause = `CAST(${ident} AS TEXT) LIKE ${quoteSqlString(`%${raw}%`)}`;
    } else if (boolTypes.has(kind)) {
      clause = `${ident} ${op} ${raw === "true" ? 1 : 0}`;
    } else if (integerTypes.has(kind)) {
      if (!/^-?\d+$/.test(raw.trim())) throw new Error(`${fieldName} 必须是整数`);
      clause = `${ident} ${op} ${raw.trim()}`;
    } else if (floatTypes.has(kind)) {
      const num = Number(raw.trim());
      if (!raw.trim() || !Number.isFinite(num)) throw new Error(`${fieldName} 必须是有限数值`);
      clause = `${ident} ${op} ${num}`;
    } else {
      clause = `${ident} ${op} ${quoteSqlString(raw)}`;
    }
  }
  el("sql-editor").value = `SELECT * FROM ${tableIdent} WHERE ${clause}`;
  await executeSql();
  syncQuickFilterValueControl();
}
async function resetQuickFilter() {
  el("filter-field").value = "";
  el("filter-op").value = "=";
  syncQuickFilterValueControl();
  if (!state.table) return;
  const sql = defaultSql();
  el("sql-editor").value = sql;
  await queryRows({ sql, order: null, reset: true });
  syncQuickFilterValueControl();
}
async function selectTable(name, { forceSchema = false } = {}) {
  if (!state.database) throw new Error("请先选择或新建数据库");
  state.queryVersion = null;
  const item = state.tables.find((entry) => entry.name === name);
  if (item?.orphan || item?.selectable === false) throw new Error(item.error || `表 ${name} 缺少可用的 SQLite 结构定义`);
  const result = (!forceSchema && item?.definition && item?.schema_version && item.row_count != null)
    ? { definition: item.definition, version: item.schema_version, row_count: item.row_count }
    : await request(tablePath("schema", name));
  state.table = name; state.definition = result.definition; state.version = result.version; state.tableTotal = result.row_count; state.schemaDirty = false;
  renderCurrentDatabaseFile();
  loadSqlHistory();
  state.offset = 0; state.fields = new Set(state.definition.columns.map((column) => column.eng_name));
  state.selected.clear(); state.selectedColumns.clear(); state.selectedCells.clear(); state.rowAnchor = state.columnAnchor = state.cellAnchor = null;
  state.records = []; state.resultColumns = []; state.lastQuery = null; state.pendingEdits.clear(); updateEditActions();
  el("data-grid").scrollTop = 0; el("data-grid").scrollLeft = 0;
  clearAddedChartSeries(); state.chartQueryKey = null;
  el("sql-editor").value = defaultSql();
  el("current-table").textContent = `${chineseName(state.definition.head)} / ${name}`;
  el("table-meta").textContent = `${state.definition.columns.length} 个字段 · 主键 ${state.definition.columns.filter((column) => column.primary_key).map((column) => column.eng_name).join(" + ")}`;
  configureQuickFilter();
  state.schemaRendered = false;
  renderTables(); renderFields();
  if (el("tab-schema")?.classList.contains("active")) renderSchema(state.definition);
  await queryRows();
  updateRecordsToolbar();
  if (!isReadOnlyDatabase()) await localDrafts.offer();
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
  resetEditHistory();
  state.queryVersion = null;
  renderedQueryStamp = null;
  state.table = null; state.definition = null; state.version = null; state.schemaIndexes = null; state.records = []; state.total = 0; state.tableTotal = 0;
  renderCurrentDatabaseFile();
  state.history = []; closeSqlHistory();
  state.selected.clear(); state.selectedColumns.clear(); state.selectedCells.clear(); state.rowAnchor = state.columnAnchor = state.cellAnchor = null;
  state.fields.clear(); state.schemaDirty = false; state.offset = 0;
  state.resultColumns = []; state.editable = false; state.lastQuery = null; state.pendingEdits.clear(); updateEditActions(); el("sql-editor").value = "";
  clearAddedChartSeries(); state.chartQueryKey = null;
  el("current-table").textContent = "请选择或新建一张表"; el("table-meta").textContent = "—";
  el("schema-fields").replaceChildren(); el("schema-name").value = ""; el("schema-label").value = "";
  configureQuickFilter();
  renderGrid(); renderFields(); configureChart(); renderTables(); updateRecordsToolbar();
}
function insertTextAtSqlCursor(snippet) {
  const input = el("sql-editor");
  if (!input) return;
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  const before = input.value.slice(0, start), after = input.value.slice(end);
  const needSpaceBefore = before && !/[\s(,=]$/.test(before);
  const inserted = (needSpaceBefore ? " " : "") + snippet;
  input.value = before + inserted + after;
  const nextPos = start + inserted.length;
  input.focus({ preventScroll: true });
  input.setSelectionRange(nextPos, nextPos);
}
function renderFields() {
  keepKeyFieldsVisible();
  const target = el("field-list"); target.replaceChildren();
  for (const column of state.resultColumns) {
    const label = node("label", null, "field-item"); const checkbox = node("input"); checkbox.type = "checkbox";
    checkbox.checked = state.fields.has(column.eng_name); checkbox.setAttribute("aria-label", `显示 ${column.eng_name}`);
    checkbox.disabled = Boolean(column.primary_key);
    label.title = column.primary_key
      ? "KEY 主键列：始终显示并冻结在左侧；双击插入字段名到 SQL"
      : `${column.eng_name}（${column.data_type || "str"}）；双击插入到 SQL`;
    checkbox.onchange = () => { if (checkbox.checked) state.fields.add(column.eng_name); else state.fields.delete(column.eng_name); renderGrid(); updateFieldCount(); };
    label.ondblclick = (event) => {
      if (event.target === checkbox) return;
      event.preventDefault();
      insertTextAtSqlCursor(quoteSqlIdent(column.eng_name));
    };
    const type = node("small", displayColumnType(column), "field-type");
    type.title = `数据类型：${column.data_type || "str"}`;
    label.append(checkbox, node("span", chineseName(column)), node("small", column.eng_name, "field-name-eng"), type, node("b", column.primary_key ? "PK" : "", "key-tag"));
    target.append(label);
  }
  updateFieldCount();
}
function updateFieldCount() { el("field-count").textContent = `${state.fields.size} / ${state.resultColumns.length}`; }
const defaultSql = () => state.table ? `SELECT * FROM "${state.table}"` : "";
async function queryRows(options = {}) {
  if (!state.table) return;
  const timing = uiPerformance.begin(options.silent ? "自动刷新" : "表格查询");
  let failure;
  try { return await queryRowsData(options,timing); }
  catch(error) { failure=error; throw error; }
  finally { uiPerformance.finish(timing,failure); }
}
async function queryRowsData({ reset = false, silent = false, sql = state.lastQuery?.sql || defaultSql(), order = state.lastQuery?.order || null, offset = state.offset, pageCursor=null } = {}, timing) {
  if (!state.table) return;
  if (hasPendingData()) { if (silent) return; throw new Error("请先保存或撤销待新增记录和单元格修改"); }
  if (reset) offset = 0;
  const result = await request(tablePath("sql"), "POST", { sql, order, readonly_only: true, compact: true,
    limit: Number(el("page-size").value), offset,
    ...(pageCursor?{page_cursor:pageCursor}:{}),
    ...(silent && state.queryVersion ? { if_version: state.queryVersion,
      ...(state.editable ? { known_records: state.records.map((record) => record.ref) } : {}) } : {}) }, timing);
  if (offset >= result.total && offset > 0) {
    offset = Math.max(0, Math.ceil(result.total / result.limit) - 1) * result.limit;
    return queryRows({ silent, sql, order, offset });
  }
  const changedPage = reset || offset !== state.offset;
  state.offset = offset; state.pageSize = result.limit;
  state.queryVersion = result.result_version || null;
  state.nextCursor=result.next_cursor||null;state.previousCursor=result.previous_cursor||null;
  if (result.unchanged) {
    timing.mode = "unchanged";
    state.total = result.total; state.tableTotal = result.table_total;
    updateGridPager(); updateQueryFeedback(result, sql); return;
  }
  if (result.delta) {
    const records = new Map(state.records.map((record) => [JSON.stringify(record.ref.key), record]));
    for (const record of result.records) records.set(JSON.stringify(record.ref.key), record);
    result.records = result.record_order.map((key) => records.get(JSON.stringify(key)));
    if (result.records.some((record) => !record)) { state.queryVersion = null; return queryRows({sql, order, offset}); }
  }
  const previousColumns = state.resultColumns;
  const chartQueryKey = JSON.stringify([state.database, state.table, sql, order, state.offset, result.limit]);
  const stamp = JSON.stringify([chartQueryKey, state.version, result.columns, result.editable,
    result.editable ? result.records.map((record) => record.ref) : result.rows]);
  if (silent && stamp === renderedQueryStamp) {
    timing.mode = "unchanged";
    state.total = result.total; state.tableTotal = result.table_total;
    updateGridPager(); updateQueryFeedback(result, sql);
    return;
  }
  const sameQuery = state.chartQueryKey === chartQueryKey;
  if (!sameQuery || !result.editable) clearAddedChartSeries();
  state.chartQueryKey = chartQueryKey;
  const changedColumns = JSON.stringify(previousColumns.map((column) => column.eng_name)) !== JSON.stringify(result.columns);
  state.resultColumns = result.columns.map((name) => {
    const defined = state.definition.columns.find((column) => column.eng_name === name);
    return defined || { eng_name: name, chn_name: name, data_type: (result.rows || []).some((row) => typeof row[name] === "number") ? "float" : "str", primary_key: false };
  });
  const patched = sameQuery && !changedPage && state.editable && result.editable &&
    JSON.stringify(previousColumns) === JSON.stringify(state.resultColumns) &&
    uiPerformance.measure(timing,"grid_ms",()=>patchGridRecords(result.records));
  if (changedColumns) state.fields = new Set(result.columns);
  state.total = result.total; state.tableTotal = result.table_total; state.editable = result.editable; updateSchemaLocks();
  state.lastQuery = { sql, order };
  if (!patched) state.records = result.editable ? result.records : result.rows.map((row) => ({ row }));
  renderedQueryStamp = stamp;
  resetEditHistory();
  if (!patched) state.records.forEach((record) => { record.baseline = structuredClone(record.row); });
  if (changedPage) el("data-grid").scrollTop = 0;
  rebindAddedChartSeries();
  state.pendingEdits.clear(); updateEditActions();
  if (!patched) {
    state.selected.clear(); state.selectedColumns.clear(); state.selectedCells.clear(); state.rowAnchor = state.columnAnchor = state.cellAnchor = null;
    resetChartCursor();
    uiPerformance.measure(timing,"grid_ms",()=>{renderFields();renderGrid();});
  } else { timing.mode = "patch"; updateGridPager(); }
  if (JSON.stringify(previousColumns) !== JSON.stringify(state.resultColumns)) { configureChart(); configureQuickFilter(); }
  else syncQuickFilterValueControl();
  updateQueryFeedback(result, sql);
  updateRecordsToolbar();
  if (!silent) log(`查询 ${state.table}：${result.total} 条，当前页 ${state.records.length} 条；${sql}`);
  uiPerformance.measure(timing,"chart_ms",()=>drawChart());
}
function updateQueryFeedback(result, sql) {
  const elapsed = Number(result.elapsed_ms || 0).toFixed(1);
  el("row-range").title = `${elapsed} ms · ${sql}`;
  el("last-refresh").textContent = `更新于 ${new Date().toLocaleTimeString()} · ${elapsed} ms`;
  const item = state.tables.find((entry) => entry.name === state.table);
  if (item && item.row_count !== result.table_total) { item.row_count = result.table_total; renderTables(); }
}
function display(value) { return value == null ? "NULL" : typeof value === "object" ? JSON.stringify(value) : String(value); }
function closeContextSubmenus(menu) {
  if (!menu) return;
  menu.querySelectorAll(".menu-group").forEach((group) => {
    group.classList.remove("is-open");
    group.querySelector(":scope > .menu-trigger")?.setAttribute("aria-expanded", "false");
    const sub = group.querySelector(":scope > .submenu");
    if (sub) sub.hidden = true;
  });
}
function syncContextSubmenuGroups(menu) {
  if (!menu) return;
  closeContextSubmenus(menu);
  menu.querySelectorAll(".menu-group").forEach((group) => {
    const trigger = group.querySelector(":scope > .menu-trigger");
    const sub = group.querySelector(":scope > .submenu");
    if (!trigger || !sub) return;
    const visibleButtons = [...sub.querySelectorAll(":scope > button")].filter((btn) => !btn.hidden);
    group.hidden = visibleButtons.length === 0;
    const allDisabled = visibleButtons.every((btn) => btn.disabled);
    trigger.disabled = allDisabled;
  });
}
function openContextSubmenu(menu, group, focusFirst = false) {
  if (!menu || !group) return;
  const trigger = group.querySelector(":scope > .menu-trigger");
  const sub = group.querySelector(":scope > .submenu");
  if (!trigger || !sub || trigger.disabled) { closeContextSubmenus(menu); return; }
  closeContextSubmenus(menu);
  group.classList.add("is-open");
  trigger.setAttribute("aria-expanded", "true");
  sub.hidden = false;
  sub.scrollTop = 0;
  const margin = 8;
  const rect = trigger.getBoundingClientRect();
  const width = sub.offsetWidth, height = sub.offsetHeight;
  let left = rect.right - 2;
  if (left + width > window.innerWidth - margin) left = Math.max(margin, rect.left - width + 2);
  const top = Math.max(margin, Math.min(rect.top - 5, window.innerHeight - height - margin));
  sub.style.left = `${left}px`;
  sub.style.top = `${top}px`;
  if (focusFirst) sub.querySelector("button:not([hidden]):not(:disabled)")?.focus({ preventScroll: true });
}
function focusFirstContextItem(menu) {
  const first = [...menu.querySelectorAll(":scope > button, :scope > .menu-group > .menu-trigger")]
    .find((btn) => !btn.hidden && !btn.disabled && !btn.closest("[hidden]"));
  (first || menu).focus({ preventScroll: true });
}
function closeAllContextMenus(except = null) {
  if (except !== "row") closeRowContextMenu();
  if (except !== "chart") closeChartContextMenu();
  if (except !== "schema") closeSchemaContextMenu();
  if (except !== "sidebar") closeSidebarContextMenu();
}
function bindContextMenuNavigation(menu, onClose) {
  if (!menu) return;
  let hoverTimer = null;
  let hoverOpenedGroup = null;
  let hoverOpenedAt = 0;
  menu.addEventListener("pointermove", (event) => {
    const group = event.target.closest(".menu-group");
    if (group && menu.contains(group)) {
      if (group.classList.contains("is-open")) {
        clearTimeout(hoverTimer); hoverTimer = null;
        return;
      }
      if (hoverOpenedGroup === group && hoverTimer !== null) return;
      clearTimeout(hoverTimer);
      hoverOpenedGroup = group;
      hoverTimer = setTimeout(() => {
        hoverTimer = null;
        if (!menu.hidden && group.isConnected) {
          openContextSubmenu(menu, group, false);
          hoverOpenedAt = performance.now();
        }
      }, 90);
      return;
    }
    if (event.target.closest(":scope > button")) {
      clearTimeout(hoverTimer); hoverTimer = null; hoverOpenedGroup = null;
      closeContextSubmenus(menu);
    }
  });
  menu.addEventListener("pointerleave", () => {
    clearTimeout(hoverTimer); hoverTimer = null; hoverOpenedGroup = null;
  });
  menu.addEventListener("click", (event) => {
    const trigger = event.target.closest(".menu-trigger");
    if (!trigger || trigger.disabled) return;
    event.stopPropagation();
    clearTimeout(hoverTimer); hoverTimer = null;
    const group = trigger.closest(".menu-group");
    // 防止刚悬停展开就点击父项导致子菜单闪烁关闭
    if (group.classList.contains("is-open")) {
      if (hoverOpenedGroup === group && performance.now() - hoverOpenedAt < 320) return;
      closeContextSubmenus(menu);
    } else {
      openContextSubmenu(menu, group, false);
    }
  });
  menu.addEventListener("keydown", (event) => {
    if (event.key === "Tab") { onClose(true); return; }
    const active = document.activeElement;
    const inSub = active?.closest(".submenu");
    if (inSub && menu.contains(inSub)) {
      if (event.key === "ArrowLeft" || event.key === "Escape") {
        event.preventDefault(); event.stopPropagation();
        const trigger = inSub.closest(".menu-group")?.querySelector(":scope > .menu-trigger");
        closeContextSubmenus(menu);
        trigger?.focus({ preventScroll: true });
        return;
      }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const buttons = [...inSub.querySelectorAll(":scope > button:not([hidden]):not(:disabled)")];
      if (!buttons.length) return;
      const current = buttons.indexOf(active);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
        : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
      return;
    }
    if ((event.key === "ArrowRight" || event.key === "Enter") && active?.classList.contains("menu-trigger") && !active.disabled) {
      event.preventDefault();
      openContextSubmenu(menu, active.closest(".menu-group"), true);
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const buttons = [...menu.querySelectorAll(":scope > button, :scope > .menu-group > .menu-trigger")]
      .filter((btn) => !btn.hidden && !btn.disabled && !btn.closest("[hidden]"));
    if (!buttons.length) return;
    const current = buttons.indexOf(active);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
      : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    closeContextSubmenus(menu);
    buttons[next].focus();
  });
}
function closeRowContextMenu(restoreFocus = false) {
  const menu = el("row-context-menu"), trigger = state.contextRecord?.trigger;
  closeContextSubmenus(menu);
  menu.hidden = true; state.contextRecord = null;
  if (restoreFocus) (trigger?.isConnected ? trigger : el("data-grid")).focus({ preventScroll: true });
}
function openRowContextMenu(event, record, index) {
  event.preventDefault(); event.stopPropagation();
  if (state.busy) return;
  closeAllContextMenus("row");
  const cell = event.target.closest("td[data-cell-field]");
  if (cell && !state.selected.has(index) && !state.selectedColumns.has(cell.dataset.cellField) && !state.selectedCells.has(cellSelectionKey(index, cell.dataset.cellField))) {
    selectGridCells(index, Number(cell.dataset.cellColumn), index, Number(cell.dataset.cellColumn));
  } else if (!cell && record && !state.selected.size && !state.selectedColumns.size && !state.selectedCells.size) selectGridRow(index, {});
  const selection = chartSelection();
  const menu = el("row-context-menu"); state.contextRecord = { record, index, selection, trigger: event.target.closest("[tabindex]") || el("data-grid") };
  const hasTable = Boolean(state.table), hasQuery = Boolean(state.lastQuery);
  const pending = hasPendingData();
  const setDisabled = (actionName, disabled) => { const btn = menu.querySelector(`[data-action="${actionName}"]`); if (btn) btn.disabled = disabled; };
  setDisabled("undo-edit", !editUndo.length); setDisabled("redo-edit", !editRedo.length);
  setDisabled("preview-edits", !hasPendingData());
  setDisabled("restore-draft",hasPendingData()||!localDrafts.candidates().length);
  setDisabled("add-row", !hasTable || isReadOnlyDatabase() || draftRecords().length >= 500);
  const rowIndexes = selectedRowIndexes(record, index);
  setDisabled("copy-rows", !state.editable || !rowIndexes.length);
  setDisabled("insert-copied-rows", !state.editable || !canInsertCopiedRows() || draftRecords().length + (state.copiedRows?.rows.length || 0) > 500);
  setDisabled("import", !hasTable || isReadOnlyDatabase() || pending);
  setDisabled("export-csv", !hasTable || !hasQuery || !state.fields.size);
  setDisabled("export-json", !hasTable || !hasQuery);
  const hasCellSelection = Boolean(gridSelectionModel());
  setDisabled("copy-cells", !hasCellSelection);
  setDisabled("paste-cells", !hasTable || !state.editable || !hasCellSelection);
  setDisabled("delete-selected", !state.editable || !rowIndexes.length);
  setDisabled("clear-table", !hasTable || pending || isReadOnlyDatabase());
  menu.querySelector('[data-action="delete-selected"]').title = !state.editable ? "当前查询结果不可删除，请切换到完整表查询" : rowIndexes.length
    ? `删除选区涉及的 ${rowIndexes.length} 条整行记录；保留其他行的待保存修改` : "请选择行或单元格区域";
  const canPlot = selection.fields.some((column) => selection.indexes.some((rowIndex) =>
    (!selection.cells.size || selection.cells.has(cellSelectionKey(rowIndex, column.eng_name))) && chartNumber(state.records[rowIndex].row[column.eng_name]) !== null));
  for (const actionName of ["chart-left", "chart-right"]) {
    const button = menu.querySelector(`[data-action="${actionName}"]`); button.disabled = !canPlot;
    button.title = canPlot ? `${selection.indexes.length} 行 × ${selection.columnCount} 列，${selection.mode === "rows" ? "按行绘制，KEY 为曲线名" : "按列绘制，列名为曲线名"}` : "选区中没有可绘制的数值";
  }
  syncContextSubmenuGroups(menu);
  menu.hidden = false;
  menu.scrollTop = 0;
  const margin = 8, width = menu.offsetWidth, height = menu.offsetHeight;
  menu.style.left = `${Math.max(margin, Math.min(event.clientX, window.innerWidth - width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(event.clientY, window.innerHeight - height - margin))}px`;
  focusFirstContextItem(menu);
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
  resetEditHistory(); state.queryVersion = null;
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
  if (actionName === "undo-edit") { restoreEdit(); return; }
  if (actionName === "redo-edit") { restoreEdit(true); return; }
  if (actionName === "preview-edits") { previewEdits(); return; }
  if (actionName === "restore-draft") { void run(()=>localDrafts.offer(true)); return; }
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
  const widths = [...grid.querySelectorAll("thead .key-column")].map((header) => [header, header.getBoundingClientRect().width]);
  widths.forEach(([header, width]) => {
    grid.style.setProperty(`--key-left-${header.dataset.frozenIndex}`, `${offset}px`);
    offset += width;
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
  state.cellAnchor = { row: startRow, column: startColumn }; clearGridSelection("cells"); scheduleGridSelection();
}
function startCellSelection(event, rowIndex, columnIndex) {
  if (state.busy || event.button !== 0 || event.target.closest(".cell-editor, .cell-toggle")) return;
  event.currentTarget.focus({ preventScroll: true });
  const additive = event.ctrlKey || event.metaKey;
  const anchor = event.shiftKey && state.cellAnchor ? { ...state.cellAnchor } : { row: rowIndex, column: columnIndex };
  const base = additive ? new Set(state.selectedCells) : new Set();
  selectGridCells(anchor.row, anchor.column, rowIndex, columnIndex, additive);
  state.cellDrag = { pointerId: event.pointerId, ...anchor, base, moved: false, lastRow: rowIndex, lastColumn: columnIndex };
  document.documentElement.classList.add("selecting-cells"); event.preventDefault();
}
function updateCellSelectionFromPointer(event) {
  const drag = state.cellDrag; if (!drag || drag.pointerId !== event.pointerId) return;
  const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-cell-row][data-cell-column]");
  if (!target) return;
  const rowIndex = Number(target.dataset.cellRow), columnIndex = Number(target.dataset.cellColumn);
  if (rowIndex === drag.lastRow && columnIndex === drag.lastColumn) return;
  drag.lastRow = rowIndex; drag.lastColumn = columnIndex;
  if (rowIndex !== drag.row || columnIndex !== drag.column) drag.moved = true;
  state.selectedCells = new Set(drag.base);
  selectGridCells(drag.row, drag.column, rowIndex, columnIndex, true);
}
function finishCellSelection(event) {
  const drag = state.cellDrag; if (!drag) return;
  state.cellDrag = null; document.documentElement.classList.remove("selecting-cells");
  if (selectionFrame !== null) paintGridSelection();
}
function gridSelectionModel() {
  const columns = visibleGridColumns();
  if (!columns.length || !state.records.length) return null;
  let rows = [], columnIndexes = [];
  if (state.selectedCells.size) {
    const positions = new Map(columns.map((column, index) => [column.eng_name, index]));
    let firstRow = Infinity, lastRow = -1, firstColumn = Infinity, lastColumn = -1;
    for (const key of state.selectedCells) {
      const { index, field } = splitCellSelectionKey(key), position = positions.get(field);
      if (!state.records[index] || position === undefined) continue;
      firstRow = Math.min(firstRow, index); lastRow = Math.max(lastRow, index);
      firstColumn = Math.min(firstColumn, position); lastColumn = Math.max(lastColumn, position);
    }
    if (lastRow < 0) return null;
    rows = rangeIndexes(firstRow, lastRow); columnIndexes = rangeIndexes(firstColumn, lastColumn);
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
  const useCellSet = state.selectedCells.size > 0;
  return model.rows.map((rowIndex) => model.columns.map((column) => (
    useCellSet && !state.selectedCells.has(cellSelectionKey(rowIndex, column.eng_name))
      ? ""
      : clipboardCellValue(state.records[rowIndex], column)
  )).join("\t")).join("\r\n");
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
  const history = editSnapshot();
  changes.forEach(({ record, column, raw }) => setPendingCell(record, column, raw, false));
  rememberEdit(history);
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
  if (selectionFrame !== null) cancelAnimationFrame(selectionFrame);
  selectionFrame = null;
  const columns = visibleGridColumns();
  const boxModel = (state.selectedCells.size || state.selectedColumns.size) ? gridSelectionModel() : null;
  const boxRows = new Set(boxModel?.rows || []), boxColumns = new Set(boxModel?.columnIndexes || []);
  const firstRow = boxModel?.rows[0], lastRow = boxModel ? boxModel.rows[boxModel.rows.length - 1] : undefined, firstColumn = boxModel?.columnIndexes[0], lastColumn = boxModel ? boxModel.columnIndexes[boxModel.columnIndexes.length - 1] : undefined;
  const nonContiguousCells = state.selectedCells.size > 0 && boxModel && state.selectedCells.size < boxModel.rows.length * boxModel.columns.length;
  gridRowEntries.forEach(({ element, index }) => element.classList.toggle("selected", state.selected.has(index)));
  gridColumnEntries.forEach(({ element, field }) => element.classList.toggle("selected-header", state.selectedColumns.has(field)));
  gridCellEntries.forEach((entry) => {
    const { cell, row, column, field, key } = entry, selected = state.selectedColumns.has(field) || state.selectedCells.has(key);
    if (entry.selected !== selected) { cell.classList.toggle("cell-selected", selected); entry.selected = selected; }
    const shadows = [];
    if (nonContiguousCells) {
      // 不连续 Ctrl 多选按各选中格的四邻精确绘制外框，避免圈入未选单元格。
      if (selected) {
        if (!state.selectedCells.has(cellSelectionKey(row - 1, field))) shadows.push("inset 0 2px var(--accent)");
        if (!state.selectedCells.has(cellSelectionKey(row + 1, field))) shadows.push("inset 0 -2px var(--accent)");
        const prevField = columns[column - 1]?.eng_name, nextField = columns[column + 1]?.eng_name;
        if (!prevField || !state.selectedCells.has(cellSelectionKey(row, prevField))) shadows.push("inset 2px 0 var(--accent)");
        if (!nextField || !state.selectedCells.has(cellSelectionKey(row, nextField))) shadows.push("inset -2px 0 var(--accent)");
      }
    } else {
      const boxed = Boolean(boxModel && boxRows.has(row) && boxColumns.has(column));
      if (boxed && row === firstRow) shadows.push("inset 0 2px var(--accent)");
      if (boxed && row === lastRow) shadows.push("inset 0 -2px var(--accent)");
      if (boxed && column === firstColumn) shadows.push("inset 2px 0 var(--accent)");
      if (boxed && column === lastColumn) shadows.push("inset -2px 0 var(--accent)");
    }
    const shadow = shadows.join(", ");
    if (entry.shadow !== shadow) { cell.style.boxShadow = shadow; entry.shadow = shadow; }
  });
  const model = boxModel || gridSelectionModel();
  const count = state.selectedCells.size || (model ? model.rows.length * model.columns.length : 0);
  let statsSuffix = "";
  if (count >= 2 && model) {
    let numCount = 0, sum = 0, min = Infinity, max = -Infinity;
    const maxSample = 2000;
    const visit = (rowIdx, field) => {
      if (numCount >= maxSample) return;
      const rec = state.records[rowIdx];
      if (!rec) return;
      const val = chartNumber(rec.row[field]);
      if (val === null || !Number.isFinite(val)) return;
      numCount += 1;
      sum += val;
      if (val < min) min = val;
      if (val > max) max = val;
    };
    if (state.selectedCells.size) {
      for (const key of state.selectedCells) {
        const { index: rowIdx, field } = splitCellSelectionKey(key);
        visit(rowIdx, field);
      }
    } else {
      for (const rowIdx of model.rows) {
        for (const col of model.columns) visit(rowIdx, col.eng_name);
      }
    }
    if (numCount >= 2) {
      const fmt = (n) => Number(n.toPrecision(6)).toString();
      statsSuffix = ` · ${t("grid.selStats", { avg: fmt(sum / numCount), min: fmt(min), max: fmt(max), sum: fmt(sum) })}`;
    }
  }
  el("selection-summary").textContent = count ? `已选 ${count.toLocaleString()} 个单元格${statsSuffix}` : "";
  updateRecordsToolbar();
  updateAutoRefreshStatus();
}
function renderGrid(windowOnly=false) {
  if(!windowOnly)finishCellSelection();
  keepKeyFieldsVisible();
  frozenKeyObserver.disconnect();
  const visible = new Set(visibleGridColumns().map((column) => column.eng_name));
  state.selectedColumns = new Set([...state.selectedColumns].filter((field) => visible.has(field)));
  state.selectedCells = new Set([...state.selectedCells].filter((key) => visible.has(splitCellSelectionKey(key).field)));
  if(!windowOnly){state.columnAnchor = state.cellAnchor = null;closeRowContextMenu();}
  const target = el("data-grid"), scrollTop = target.scrollTop, scrollLeft = target.scrollLeft;
  const oldCells=windowOnly?gridCells:null,oldRows=new Map(windowOnly?gridRowEntries.filter(entry=>entry.element.tagName==='TR').map(entry=>[entry.index,entry.element]):[]);
  const focus=document.activeElement?.closest('#data-grid td[data-cell-row]');
  const focused=focus?{row:Number(focus.dataset.cellRow),field:focus.dataset.cellField}:null;
  if(windowOnly&&document.activeElement?.classList.contains('cell-editor'))document.activeElement.blur();
  const existingGrid = windowOnly ? target.querySelector("table.virtual-grid") : null;
  if (!state.resultColumns.length) {
    gridCells = new WeakMap(); gridCellEntries = []; gridRowEntries = []; gridColumnEntries = [];
    const empty = node("div", null, "empty");
    empty.append(node("p", state.database ? "已选择数据库，可从左侧选择表或新建表。" : "先选择或新建数据库，再管理数据表和记录。"));
    if (!state.database) empty.append(action("选择 / 新建数据库", () => run(openDatabaseFileDialog), "secondary"));
    target.replaceChildren(empty);
  }
  else {
    const columns = visibleGridColumns();
    const viewport=virtualGrid.plan(columns);
    // 虚拟滚动纯垂直移动时复用已有 <table> 与 <thead>，仅增量替换 <tbody> 消除表头重建与回流抖动。
    const reuseTable = Boolean(
      windowOnly && virtualGrid.active && existingGrid &&
      existingGrid.dataset.columnSignature === viewport.columnSignature
    );
    gridCells = new WeakMap(); gridCellEntries = []; gridRowEntries = [];
    if (!reuseTable) gridColumnEntries = [];
    let grid, body;
    if (reuseTable) {
      grid = existingGrid;
      body = grid.querySelector("tbody");
      body.replaceChildren();
    } else {
      grid = node("table");
      const head = node("tr"), thead = node("thead");
      grid.setAttribute("aria-label", state.table || "查询结果");
      grid.classList.toggle('virtual-grid',virtualGrid.active);
      grid.dataset.columnSignature = viewport.columnSignature;
      grid.setAttribute('aria-rowcount',String(state.records.length+1));grid.setAttribute('aria-colcount',String(columns.length+1));
      if(virtualGrid.active){
        grid.style.width=(36+columns.length*virtualGrid.columnWidth)+'px';
        const group=node('colgroup'),corner=node('col');corner.style.width='36px';group.append(corner);
        for(const segment of viewport.segments){const col=node('col');col.style.width=segment.width+'px';group.append(col);}
        grid.append(group);
      }
      {
        const corner = node("th", "↖", "row-corner"); corner.scope = "col"; corner.title = "选择当前页全部行和列"; corner.setAttribute("aria-label", "选择当前页全部单元格");
        corner.onclick = () => { state.selected = new Set(state.records.map((_record, index) => index)); state.selectedColumns = new Set(columns.map((column) => column.eng_name)); state.selectedCells.clear(); state.rowAnchor = state.records.length - 1; state.columnAnchor = columns.length - 1; state.cellAnchor = null; paintGridSelection(); };
        head.append(corner);
      }
      viewport.segments.forEach(({column,index:columnIndex,gap,width}) => {
        if(gap){const spacer=node('th',null,'virtual-gap');spacer.style.width=width+'px';spacer.setAttribute('aria-hidden','true');head.append(spacer);return;}
        const header = node("th", null, `column-header${state.selectedColumns.has(column.eng_name) ? " selected-header" : ""}${isNumericColumn(column) ? " is-numeric" : ""}`); header.scope = "col"; header.dataset.gridColumn = column.eng_name; header.tabIndex = 0;
        gridColumnEntries.push({ element: header, field: column.eng_name });
        const sorted = state.lastQuery?.order?.column === column.eng_name, direction = state.lastQuery?.order?.direction;
        const sorting = sorted ? direction === "ASC" ? " ↑" : " ↓" : "";
        header.setAttribute("aria-sort", sorted ? direction === "ASC" ? "ascending" : "descending" : "none");
        header.title = `${chineseName(column)} / ${column.eng_name} · ${displayColumnType(column)}；单击选择，双击排序`;
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
          // 列头双击按 升序 -> 降序 -> 取消排序 三态循环。
          const nextOrder = !sorted ? { column: column.eng_name, direction: "ASC" }
            : direction === "ASC" ? { column: column.eng_name, direction: "DESC" } : null;
          await queryRows({ reset: true, order: nextOrder });
        });
        if (!virtualGrid.active) {
          const widthKey = `${state.table || "_"}:${column.eng_name}`;
          const savedW = loadColumnWidths()[widthKey];
          if (savedW) { header.style.width = `${savedW}px`; header.style.minWidth = `${savedW}px`; }
          const resizer = node("span", null, "col-resize-handle");
          resizer.setAttribute("aria-hidden", "true");
          resizer.onclick = (event) => event.stopPropagation();
          resizer.ondblclick = (event) => {
            event.stopPropagation();
            header.style.width = ""; header.style.minWidth = "";
            saveColumnWidth(widthKey, null);
          };
          resizer.onpointerdown = (event) => {
            event.preventDefault(); event.stopPropagation();
            const startX = event.clientX, startW = header.getBoundingClientRect().width;
            resizer.classList.add("is-resizing");
            const onMove = (moveEv) => {
              const nextW = Math.max(56, Math.min(640, Math.round(startW + (moveEv.clientX - startX))));
              header.style.width = `${nextW}px`; header.style.minWidth = `${nextW}px`;
            };
            const onUp = () => {
              resizer.classList.remove("is-resizing");
              document.removeEventListener("pointermove", onMove);
              document.removeEventListener("pointerup", onUp);
              saveColumnWidth(widthKey, Math.round(header.getBoundingClientRect().width));
            };
            document.addEventListener("pointermove", onMove);
            document.addEventListener("pointerup", onUp);
          };
          header.append(resizer);
        }
        if (el("chinese-columns").checked && chineseName(column) !== column.eng_name) header.append(node("small", column.eng_name)); head.append(header);
      });
      thead.append(head); grid.append(thead);
      body = node("tbody");
    }
    const spacerRow=height=>{if(!height)return;const row=node('tr',null,'virtual-spacer'),cell=node('td');cell.colSpan=viewport.segments.length+1;cell.style.height=height+'px';row.setAttribute('aria-hidden','true');row.append(cell);body.append(row);};
    spacerRow(viewport.first*virtualGrid.rowHeight);
    state.records.slice(viewport.first,viewport.last).forEach((record, offset) => {
      const index=viewport.first+offset, row=oldRows.get(index)||node('tr');row.classList.toggle('selected',state.selected.has(index));row.dataset.gridRow=String(index);row.setAttribute('aria-rowindex',String(index+2));
      const children=[];
      gridRowEntries.push({ element: row, index });
      const recordCells = new Map(); gridCells.set(record, recordCells);
      row.classList.toggle("draft-row", Boolean(record.draftId));
      row.oncontextmenu = (event) => openRowContextMenu(event, record, index);
      {
        const rowHeader = node("th", record.draftId ? `新${record.draftId}` : String(state.offset + index + 1), "row-header"); rowHeader.scope = "row"; rowHeader.dataset.gridRow = String(index); rowHeader.title = record.draftId ? "待新增记录，保存后写入数据库" : `选择第 ${state.offset + index + 1} 行：${record.ref?.key.join(" / ") || ""}`;
        rowHeader.onclick = (event) => selectGridRow(index, event); children.push(rowHeader);
        gridRowEntries.push({ element: rowHeader, index });
      }
      viewport.segments.forEach(({column,index:columnIndex,gap,width}) => {
        if(gap){const spacer=node('td',null,'virtual-gap');spacer.style.width=width+'px';spacer.setAttribute('aria-hidden','true');children.push(spacer);return;}
        const cell = oldCells?.get(record)?.get(column.eng_name)||node("td", null, "value"); cell.dataset.cellRow = String(index); cell.dataset.cellColumn = String(columnIndex); cell.dataset.cellField = column.eng_name;cell.setAttribute('aria-colindex',String(columnIndex+2));
        cell.tabIndex = -1;
        recordCells.set(column.eng_name, cell);
        gridCellEntries.push({ cell, row: index, column: columnIndex, field: column.eng_name, key: cellSelectionKey(index, column.eng_name) });
        if (column.primary_key) markFrozenKey(cell, columnIndex);
        cell.classList.toggle("cell-selected", state.selectedCells.has(cellSelectionKey(index, column.eng_name)));
        cell.onpointerdown = (event) => { startCellSelection(event, index, columnIndex); };
        renderGridCellValue(record, column, cell);
        if (state.editable && !isReadOnlyKey(record, column)) cell.ondblclick = (event) => {
          if (event.target.closest(".cell-editor, .cell-toggle") || state.busy) return;
          event.preventDefault();
          if (!inlineCellInput(record, column, index, cell)) run(() => openRow("edit", record));
        };
        children.push(cell);
      });
      row.replaceChildren(...children);
      body.append(row);
    });
    spacerRow((state.records.length-viewport.last)*virtualGrid.rowHeight);
    if (!state.records.length) { const row = node("tr"), cell = node("td", "当前 SQL 查询没有记录。", "empty"); cell.colSpan = columns.length + 1; row.append(cell); body.append(row); }
    if (!reuseTable) {
      grid.append(body); target.replaceChildren(grid);
      if(virtualGrid.active){for(let i=0;i<viewport.keys;i++)grid.style.setProperty(`--key-left-${i}`,`${36+i*virtualGrid.columnWidth}px`);}
      else{frozenKeyObserver.observe(grid);grid.querySelectorAll("thead .key-column, .row-corner").forEach((header) => frozenKeyObserver.observe(header));updateFrozenKeyOffsets();}
    }
  }
  paintGridSelection();
  if (!windowOnly) { target.scrollTop = scrollTop; target.scrollLeft = scrollLeft; }
  if(windowOnly&&focused)gridCells.get(state.records[focused.row])?.get(focused.field)?.focus({preventScroll:true});
  if(!windowOnly){updateEditActions();updateGridPager();}
}
function updateGridPager() {
  const pageSize = Number(el("page-size").value), pages = Math.max(1, Math.ceil(state.total / pageSize));
  el("page-number").value = String(Math.floor(state.offset / pageSize) + 1); el("page-number").max = String(pages); el("page-total").textContent = pages;
  el("prev").disabled = state.offset === 0; el("next").disabled = state.offset + pageSize >= state.total;
  const drafts = draftRecords().length;
  const savedCount = state.records.length - drafts;
  el("row-range").textContent = (savedCount ? `${state.offset + 1}–${state.offset + savedCount} / ${state.total.toLocaleString()} 条` : `当前页 0 / ${state.total.toLocaleString()} 条`) + (drafts ? ` · 新增 ${drafts} 条待保存` : "");
}
function moveGridFocus(row, column, key, shift = false, control = false, extend = shift) {
  const columns = visibleGridColumns(), lastRow = state.records.length - 1, lastColumn = columns.length - 1;
  if (lastRow < 0 || lastColumn < 0) return;
  let nextRow = row, nextColumn = column;
  if (key === "Home" || key === "End") {
    nextColumn = key === "Home" ? 0 : lastColumn;
    if (control) nextRow = key === "Home" ? 0 : lastRow;
  } else if (key === "Tab") {
    const position = Math.max(0, Math.min(lastRow * columns.length + lastColumn, row * columns.length + column + (shift ? -1 : 1)));
    nextRow = Math.floor(position / columns.length); nextColumn = position % columns.length;
  } else if (key === "Enter") nextRow += shift ? -1 : 1;
  else if (key === "ArrowUp") nextRow--;
  else if (key === "ArrowDown") nextRow++;
  else if (key === "ArrowLeft") nextColumn--;
  else if (key === "ArrowRight") nextColumn++;
  nextRow = Math.max(0, Math.min(lastRow, nextRow)); nextColumn = Math.max(0, Math.min(lastColumn, nextColumn));
  const anchor = extend && state.cellAnchor ? state.cellAnchor : { row: nextRow, column: nextColumn };
  selectGridCells(anchor.row, anchor.column, nextRow, nextColumn);
  const target = virtualGrid.ensure(nextRow,nextColumn);
  target?.focus({ preventScroll: true });
  target?.scrollIntoView({ block: "nearest", inline: "nearest" });
}
function openAddRows(record, index) {
  requireTable();
  const remaining = 500 - draftRecords().length;
  if (remaining <= 0) throw new Error("一次最多暂存 500 条新增记录，请先保存或撤销");
  state.addRowIndex = record ? index + 1 : null;
  const preset = el("add-row-preset"), countInput = el("add-row-count");
  countInput.max = String(remaining);
  if (preset) {
    for (const option of preset.options) {
      if (option.value !== "custom") option.disabled = Number(option.value) > remaining;
    }
    syncPresetControl(preset, countInput, 1);
  } else {
    countInput.value = "1";
  }
  el("add-rows-error").textContent = ""; el("add-rows-dialog").showModal();
  if (preset && !preset.hidden) preset.focus({ preventScroll: true });
  else { countInput.focus(); countInput.select(); }
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
  const history = editSnapshot();
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
  rememberEdit(history);
  state.selected = new Set(copies.map((_copy, copyIndex) => start + copyIndex));
  state.selectedColumns.clear(); state.selectedCells.clear();
  rebindAddedChartSeries(); renderGrid(); drawChart();
  const cell = virtualGrid.ensure(start,0);
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
  const history = editSnapshot();
  const first = state.addRowIndex === null ? state.records.length : Math.min(state.addRowIndex, state.records.length);
  const rows = Array.from({ length: count }, () => {
    const row = Object.fromEntries(state.definition.columns.map((column) => [column.eng_name, fieldDefault(column)]));
    return { draftId: ++state.draftSequence, row, baseline: structuredClone(row) };
  });
  state.records.splice(first, 0, ...rows);
  rememberEdit(history);
  state.selected.clear(); state.selectedColumns.clear(); state.selectedCells.clear();
  state.rowAnchor = state.columnAnchor = state.cellAnchor = null;
  rebindAddedChartSeries(); renderGrid(); drawChart(); el("add-rows-dialog").close(); tab("data");
  const cell = virtualGrid.ensure(first,0);
  cell?.scrollIntoView({ block: "nearest", inline: "nearest" }); cell?.focus({ preventScroll: true });
  notice(`已加入 ${count} 条新记录，已填入字段默认值；请双击填写 KEY 和其他字段，再保存修改`);
  log(`${state.table}：按字段默认值暂存新增 ${count} 条记录，尚未写入数据库`);
}
function fieldDefault(column) {
  return structuredClone(column.default_value ?? null);
}
const ROW_NULL_SENTINEL = "__RTDB_NULL__";
function createFieldEditorControl(column, initialValue, { allowNullOption = false } = {}) {
  const kind = column.data_type.toLowerCase();
  const menu = columnMenuEntries(column);
  const isSelect = boolTypes.has(kind) || menu.length > 0;
  const input = node(isSelect ? "select" : objectTypes.has(kind) ? "textarea" : "input");
  input.dataset.field = column.eng_name;
  input.setAttribute("aria-label", `字段 ${column.eng_name}`);
  if (isSelect) {
    if (allowNullOption) input.append(new Option("（空值 NULL）", ROW_NULL_SENTINEL));
    if (boolTypes.has(kind)) {
      input.append(new Option("是 / true", "true"), new Option("否 / false", "false"));
    } else {
      const seen = new Set();
      for (const item of menu) {
        if (seen.has(item.value)) continue;
        seen.add(item.value);
        input.append(new Option(item.label, item.value));
      }
      if (initialValue != null && String(initialValue) !== "" && !seen.has(String(initialValue))) {
        input.append(new Option(`${initialValue}（当前）`, String(initialValue)));
      }
    }
    if (initialValue == null) {
      input.value = allowNullOption ? ROW_NULL_SENTINEL : (input.options[0]?.value ?? "");
    } else {
      const text = String(initialValue);
      if (![...input.options].some((option) => option.value === text)) input.append(new Option(`${text}（当前）`, text));
      input.value = text;
    }
  } else {
    input.value = initialValue == null ? "" : objectTypes.has(kind) ? JSON.stringify(initialValue) : String(initialValue);
  }
  return { input, isSelect };
}
function parseDialogFieldValue(column, input, nullable) {
  const kind = column.data_type.toLowerCase();
  if (nullable?.checked || (input.tagName === "SELECT" && input.value === ROW_NULL_SENTINEL)) return null;
  const value = input.value;
  if (integerTypes.has(kind)) {
    if (!/^-?\d+$/.test(value.trim())) throw new Error(`${column.eng_name} 必须是整数`);
    const big = BigInt(value.trim());
    if (isIntBoolDisplayColumn(column) && big !== 0n && big !== 1n) {
      throw new Error(`${column.eng_name} 布尔类型只能取值 0 或 1`);
    }
    return big.toString();
  }
  if (floatTypes.has(kind)) { if (!value.trim() || !Number.isFinite(Number(value))) throw new Error(`${column.eng_name} 必须是有限数值`); return Number(value); }
  if (boolTypes.has(kind)) return value === "true";
  if (objectTypes.has(kind)) return JSON.parse(value);
  return value;
}
function batchEditableColumns() {
  return (state.dialog?.definition?.columns || []).filter((column) => !column.primary_key);
}
function refreshBatchFieldPicker() {
  const select = el("batch-field-select"), addBtn = el("batch-add-field");
  if (!select) return;
  const used = new Set([...el("batch-fields-list").querySelectorAll(".batch-field-row")].map((row) => row.dataset.batchField));
  const available = batchEditableColumns().filter((column) => !used.has(column.eng_name));
  select.replaceChildren(new Option(available.length ? "选择要修改的字段…" : "已添加全部可编辑字段", ""));
  for (const column of available) select.append(new Option(`${chineseName(column)} / ${column.eng_name}`, column.eng_name));
  select.disabled = !available.length;
  if (addBtn) addBtn.disabled = !available.length;
}
function addBatchFieldRow(fieldName, initialValue = undefined) {
  const column = batchEditableColumns().find((item) => item.eng_name === fieldName);
  if (!column) return;
  const list = el("batch-fields-list");
  if (list.querySelector(`.batch-field-row[data-batch-field="${CSS.escape(fieldName)}"]`)) return;
  const row = node("div", null, "batch-field-row");
  row.dataset.batchField = fieldName;
  const header = node("div", null, "batch-field-head");
  header.append(node("strong", `${chineseName(column)} / ${column.eng_name}`));
  const remove = action("×", () => { row.remove(); refreshBatchFieldPicker(); }, "icon");
  remove.setAttribute("aria-label", `移除字段 ${column.eng_name}`);
  header.append(remove);
  const value = initialValue === undefined ? fieldDefault(column) : initialValue;
  const { input, isSelect } = createFieldEditorControl(column, value, { allowNullOption: true });
  row.append(header, input);
  if (!isSelect) {
    const nullLabel = node("label", null, "check-label"), checkbox = node("input");
    checkbox.type = "checkbox"; checkbox.dataset.nullField = column.eng_name;
    checkbox.checked = value == null; input.disabled = checkbox.checked;
    checkbox.onchange = () => { input.disabled = checkbox.checked; };
    nullLabel.append(checkbox, node("span", "空值 NULL"));
    row.append(nullLabel);
  }
  list.append(row);
  refreshBatchFieldPicker();
  if (!input.disabled) input.focus({ preventScroll: true });
}
function buildBatchFields(values = {}) {
  const list = el("batch-fields-list");
  list.replaceChildren();
  for (const [field, value] of Object.entries(values || {})) {
    addBatchFieldRow(field, value);
  }
  refreshBatchFieldPicker();
}
function readBatchFields() {
  const result = {};
  const columns = new Map(batchEditableColumns().map((column) => [column.eng_name, column]));
  for (const row of el("batch-fields-list").querySelectorAll(".batch-field-row")) {
    const field = row.dataset.batchField, column = columns.get(field);
    if (!column) continue;
    const input = row.querySelector("[data-field]"), nullable = row.querySelector("[data-null-field]");
    result[field] = parseDialogFieldValue(column, input, nullable);
  }
  if (!Object.keys(result).length) throw new Error("请至少添加一个要修改的字段");
  return result;
}
function openRow(mode, record = null) {
  requireTable();
  if (hasPendingData()) throw new Error("请先保存或撤销待新增记录和单元格修改");
  if (mode !== "add" && !state.editable) throw new Error("请使用 SQL 修改，或重置后选择原始记录");
  const rows = mode === "batch"
    ? selectedRowIndexes(record, null).map((index) => state.records[index]).filter((item) => item && !item.draftId)
    : record ? [record] : [];
  if (mode === "batch" && !rows.length) throw new Error("请先选择已保存的记录");
  state.dialog = { mode, table: state.table, records: structuredClone(rows), definition: structuredClone(state.definition) };
  el("row-dialog-title").textContent = ({ add: "新增记录", edit: "编辑记录", copy: "复制记录", batch: "批量修改" })[mode];
  const rowHint = el("row-hint");
  rowHint.textContent = mode === "batch" ? `已选 ${rows.length} 条记录` : "";
  rowHint.hidden = !rowHint.textContent;
  el("row-error").textContent = "";
  const values = mode === "batch" ? {} : record ? structuredClone(record.row) : Object.fromEntries(state.definition.columns.map((column) => [column.eng_name, fieldDefault(column)]));
  el("row-editor").value = JSON.stringify(values, null, 2);
  el("json-mode").checked = false; el("json-mode").disabled = false;
  if (mode === "batch") {
    buildBatchFields(values);
    el("row-fields").hidden = true; el("batch-fields-panel").hidden = false; el("row-editor").hidden = true;
  } else {
    buildRowFields(values);
    el("row-fields").hidden = false; el("batch-fields-panel").hidden = true; el("row-editor").hidden = true;
  }
  el("row-dialog").showModal();
}
function buildRowFields(values) {
  el("row-fields").replaceChildren();
  for (const column of state.dialog.definition.columns) {
    const label = node("label", `${chineseName(column)} / ${column.eng_name}${column.primary_key ? " · 主键" : ""}`);
    const allowNullOption = !column.primary_key;
    const { input, isSelect } = createFieldEditorControl(column, values[column.eng_name], { allowNullOption });
    input.disabled = state.dialog.mode === "edit" && column.primary_key;
    label.append(input);
    if (!column.primary_key && !isSelect) {
      const nullLabel = node("label", null, "check-label"), checkbox = node("input"); checkbox.type = "checkbox";
      checkbox.dataset.nullField = column.eng_name; checkbox.checked = values[column.eng_name] == null; input.disabled = checkbox.checked;
      checkbox.onchange = () => { input.disabled = checkbox.checked; }; nullLabel.append(checkbox, node("span", "空值 NULL")); label.append(nullLabel);
    }
    el("row-fields").append(label);
  }
}
function readRowFields() {
  if (state.dialog?.mode === "batch") return readBatchFields();
  const result = {};
  for (const column of state.dialog.definition.columns) {
    const input = [...el("row-fields").querySelectorAll("[data-field]")].find((field) => field.dataset.field === column.eng_name);
    const nullable = [...el("row-fields").querySelectorAll("[data-null-field]")].find((field) => field.dataset.nullField === column.eng_name);
    result[column.eng_name] = parseDialogFieldValue(column, input, nullable);
  }
  return result;
}
function showSaveWarning(error) {
  const message = error.message || String(error);
  el("save-warning-list").replaceChildren(...message.split("\n").filter(Boolean).map((line) => node("li", line)));
  renderSaveProblems(error);
  if (!el("save-warning-dialog").open) el("save-warning-dialog").showModal();
  notice("保存检查未通过，请修正提示的问题后再保存", true); log(message, true);
}
async function saveValidatedEdits(payload, table = state.table, timing = null) {
  try {
    return await request(tablePath("cell-edits", table), "PUT", payload, timing);
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
    try { groups.get(key).values[edit.column.eng_name] = parseCellValue(edit.column, edit.raw); }
    catch (error) { error.editTarget = {record: edit.record, field: edit.column.eng_name}; throw error; }
  }
  const inserts = draftRecords().map((record, index) => {
    const values = {};
    for (const column of state.definition.columns) {
      const pending = pendingCell(record, column.eng_name), raw = pending ? pending.raw : cellText(record.row[column.eng_name]);
      try { values[column.eng_name] = pending ? parseCellValue(column, raw) : record.row[column.eng_name]; }
      catch (error) { error.message = `新增第 ${index + 1} 行：${error.message}`; error.editTarget = {record, field:column.eng_name}; throw error; }
    }
    return values;
  });
  return { changes: [...groups.values()].map((group) => ({ ref: group.record.ref, values: group.values })), inserts };
}
async function saveCellEdits() {
  if (!hasPendingData()) return;
  let payload;
  try { payload = pendingEditPayload(); } catch (error) { showSaveWarning(error); return; }
  const measurement=uiPerformance.begin('保存并刷新');let saved=false;
  try {
  const result = await saveValidatedEdits(payload,state.table,measurement);
  if (!result) return;
  state.records = state.records.filter((record) => !record.draftId);
  state.pendingEdits.clear();
  updateEditActions();
  const message = result.inserted ? `已新增 ${result.inserted} 条记录${result.updated ? `，修改 ${result.updated} 条记录` : ""}` : `已保存 ${result.updated} 条记录`;
  log(`${state.table}：${message}`); notice(message); await queryRows();
  saved=true;
  } finally {uiPerformance.finish(measurement,!saved);}
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
  state.pendingEdits.clear(); resetEditHistory(); rebindAddedChartSeries(); updateEditActions(); renderGrid(); drawChart(); notice("已撤销待新增记录和单元格修改");
  return true;
}
function schemaFormValue(withMeta = false) {
  const head = { eng_name: el("schema-name").value.trim(), chn_name: el("schema-label").value.trim() };
  head.chn_name = chineseName(head);
  const renames=Object.fromEntries([...el('schema-fields').querySelectorAll('tbody tr[data-original-name]')].map(row=>[row.dataset.originalName,row.querySelector('[data-property="eng_name"]').value.trim()]));
  return { head, ...(state.schemaIndexes!=null?{indexes:state.schemaIndexes.map(fields=>fields.map(name=>renames[name]||name))}:{}),
    columns: [...el("schema-fields").querySelectorAll("tbody tr")].map((row) => {
      const column = Object.fromEntries([...row.querySelectorAll("[data-property]")].map((input) => [input.dataset.property, input.type === "checkbox" ? input.checked : input.value]));
      const defaultControl = row.querySelector('[data-property="default_value"]');
      const isSelectDefault = defaultControl?.tagName === "SELECT";
      column.chn_name = chineseName(column);
      const allowedDisplayTypes = schemaDisplayTypesFor(column.data_type);
      if (!allowedDisplayTypes.includes(column.disp_type)) {
        throw new Error(`${column.eng_name} 的数据类型 ${column.data_type} 不支持显示类型「${column.disp_type}」`);
      }
      if (![TIME_DISPLAY_TYPE, MENU_DISPLAY_TYPE, CHOICE_DISPLAY_TYPE].includes(column.disp_type)) {
        column.disp_menu = "";
      }
      if (column.disp_type === TIME_DISPLAY_TYPE && isIntegerSchemaType(column.data_type) && !String(column.disp_menu || "").trim()) {
        column.disp_menu = DEFAULT_TIME_FORMAT;
      }
      if (column.disp_type === MENU_DISPLAY_TYPE && isIntegerSchemaType(column.data_type) && String(column.disp_menu || "").trim()) {
        try {
          const parsed = JSON.parse(String(column.disp_menu));
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("必须为 JSON 对象");
          for (const key of Object.keys(parsed)) {
            const trimmed = String(key).trim();
            if (!/^-?\d+$/.test(trimmed)) throw new Error(`菜单 Key "${key}" 必须为整数`);
            const big = BigInt(trimmed);
            if (big < -(2n ** 63n) || big >= 2n ** 63n) throw new Error(`菜单 Key "${key}" 超出 64 位整数范围`);
          }
        } catch (error) {
          throw new Error(`${column.eng_name} 菜单选项格式无效（${error.message}），请双击选项列重新配置`);
        }
      }
      if (column.disp_type === CHOICE_DISPLAY_TYPE && isChoiceSchemaType(column.data_type) && String(column.disp_menu || "").trim()) {
        try {
          const rawChoice = String(column.disp_menu).trim();
          const isInt = isIntegerSchemaType(column.data_type);
          const seen = new Set();
          if (isInt) {
            const exactTokens = parseIntegerJsonArrayTokens(rawChoice);
            if (exactTokens === null) {
              const parsed = JSON.parse(rawChoice);
              if (!Array.isArray(parsed)) throw new Error("必须为 JSON 数组");
              throw new Error("整数类型字段的选择选项只能为整数");
            }
            for (const token of exactTokens) {
              const big = BigInt(token);
              if (big < -(2n ** 63n) || big >= 2n ** 63n) throw new Error(`选项 "${token}" 超出 64 位整数范围`);
              const key = big.toString();
              if (seen.has(key)) throw new Error(`选项 "${key}" 重复`);
              seen.add(key);
            }
          } else {
            const parsed = JSON.parse(rawChoice);
            if (!Array.isArray(parsed)) throw new Error("必须为 JSON 数组");
            for (const item of parsed) {
              if (typeof item !== "string") throw new Error("字符串类型字段的选择选项只能为字符串");
              if (seen.has(item)) throw new Error(`选项 "${item}" 重复`);
              seen.add(item);
            }
          }
        } catch (error) {
          throw new Error(`${column.eng_name} 选择选项格式无效（${error.message}），请双击选项列重新配置`);
        }
      }
      const rawDefault = column.default_value;
      delete column.default_value;
      if (rawDefault.trim()) {
        try {
          const kind = columnKind(column);
          const isStringKind = !integerTypes.has(kind) && !floatTypes.has(kind) && !boolTypes.has(kind) && !objectTypes.has(kind);
          const value = isSelectDefault && isStringKind
            ? rawDefault
            : isStringKind && rawDefault.trim().startsWith('"')
              ? JSON.parse(rawDefault) : parseCellValue(column, rawDefault);
          if (column.primary_key && (value == null || typeof value === "string" && !value.trim())) throw new Error("主键默认值不能为空");
          column.default_value = value;
        } catch (error) { throw new Error(`${column.eng_name} 默认值：${error.message}`); }
      }
      if (withMeta && row.dataset.originalName) column.__original_name = row.dataset.originalName;
      return column;
    }) };
}
function schemaChanged() { state.schemaDirty = true; localDrafts.schedule(); updateAutoRefreshStatus(); }
function schemaDisplayTypesFor(kind) {
  const type = String(kind || "str").toLowerCase();
  if (isIntegerSchemaType(type)) return ["整数", BOOL_DISPLAY_TYPE, TIME_DISPLAY_TYPE, MENU_DISPLAY_TYPE, CHOICE_DISPLAY_TYPE];
  if (floatTypes.has(type)) return ["浮点数"];
  if (boolTypes.has(type)) return [BOOL_DISPLAY_TYPE];
  if (isStringSchemaType(type)) return ["文本", CHOICE_DISPLAY_TYPE];
  return ["文本"];
}
function schemaDefaultDisplayType(kind) {
  const type = String(kind || "str").toLowerCase();
  if (isIntegerSchemaType(type)) return "整数";
  if (floatTypes.has(type)) return "浮点数";
  if (boolTypes.has(type)) return BOOL_DISPLAY_TYPE;
  return "文本";
}
function migrateLegacyDropdownMenu(rawMenu, dataType) {
  const text = String(rawMenu ?? "").trim();
  if (!text) return "";
  if (text.startsWith("[")) return serializeChoiceSet(parseChoiceSet(text, dataType), dataType);
  const parts = text.split("|").map((item) => item.trim()).filter(Boolean);
  return serializeChoiceSet(parts, dataType);
}
function normalizeSchemaDisplayType(raw, dataType, dispMenu = "") {
  const fallback = schemaDefaultDisplayType(dataType);
  const text = String(raw ?? "").trim();
  const allowed = schemaDisplayTypesFor(dataType);
  if (!text) {
    return String(dispMenu || "").trim() && isChoiceSchemaType(dataType) ? CHOICE_DISPLAY_TYPE : fallback;
  }
  if (allowed.includes(text)) return text;
  const lower = text.toLowerCase();
  if (isIntegerSchemaType(dataType) && (text === TIME_DISPLAY_TYPE || text === "时间" || lower === "time" || lower === "timestamp")) {
    return TIME_DISPLAY_TYPE;
  }
  if (isIntegerSchemaType(dataType) && (text === MENU_DISPLAY_TYPE || text === "菜单" || lower === "menu_dict" || lower === "enum_dict")) {
    return MENU_DISPLAY_TYPE;
  }
  if (isChoiceSchemaType(dataType) && (text === CHOICE_DISPLAY_TYPE || text === "选择" || text === "下拉框" || ["choice_set", "set", "select", "combobox", "combo", "menu", "enum", "dropdown"].includes(lower))) {
    return CHOICE_DISPLAY_TYPE;
  }
  if ((isIntegerSchemaType(dataType) || boolTypes.has(String(dataType || "").toLowerCase())) && (text === BOOL_DISPLAY_TYPE || ["bool", "boolean"].includes(lower))) {
    return BOOL_DISPLAY_TYPE;
  }
  // 其他不匹配的数据类型与显示类型组合一律回退至该数据类型的合理默认显示类型
  return fallback;
}
function isReadOnlyDatabase() {
  return state.databases.some((item) => item.filename === state.database && item.read_only);
}
function isNativeDatabase() {
  return state.databases.some((item) => item.filename === state.database && item.storage_format === "native");
}
function isLockedNativeField(_row, _property) {
  // 原生 SQLite 表已支持通过原子事务安全修改字段类型、主键和默认值，不再锁定已有列。
  return false;
}
function updateSchemaLocks() {
  const readOnly = isReadOnlyDatabase();
  for (const id of ["new-table", "save-schema", "add-column", "schema-name", "schema-label", "delete-schema", "schema-preview-btn"]) {
    if (el(id)) el(id).disabled = !state.database || readOnly;
  }
  if (el("schema-indexes-btn")) {
    el("schema-indexes-btn").disabled = !state.database || readOnly || isNativeDatabase();
    el("schema-indexes-btn").title = isNativeDatabase() ? "原生 SQLite 的索引由原表定义" : "";
  }
  for (const btn of el("schema-fields").querySelectorAll(".schema-row-actions button")) {
    btn.disabled = readOnly;
  }
  const deleteSchemaButton = el("delete-schema");
  if (!deleteSchemaButton.disabled && (!state.table || state.tableTotal > 0)) {
    deleteSchemaButton.disabled = true;
    deleteSchemaButton.title = !state.table ? "请先选择已有表" : "仅允许删除空表结构，当前表有数据";
  } else {
    deleteSchemaButton.title = "";
  }
  const hint = el("schema-hint");
  hint.textContent = readOnly ? "当前数据库只读" : "";
  hint.hidden = !hint.textContent;
  for (const row of el("schema-fields").querySelectorAll("tbody tr")) {
    const editing = row.dataset.editing === "true";
    for (const input of row.querySelectorAll("[data-property]")) {
      const cell = input.closest("td");
      if (input.type === "hidden") continue;
      if (input.classList.contains("schema-menu-none")) {
        input.disabled = true;
        input.title = "";
        if (cell) {
          cell.classList.add("schema-locked");
          cell.title = "";
        }
        continue;
      }
      if (input.classList.contains("schema-menu-json") || input.classList.contains("schema-choice-json")) {
        input.disabled = readOnly;
        const tip = readOnly ? "当前数据库只读，不可修改" : input.classList.contains("schema-choice-json") ? "双击配置选择选项" : "双击配置菜单选项";
        input.title = tip;
        if (cell) {
          cell.classList.toggle("schema-locked", readOnly);
          cell.title = tip;
        }
        continue;
      }
      const directEdit = input.tagName === "SELECT" || ["checkbox", "radio"].includes(input.type);
      const locked = isLockedNativeField(row, input.dataset.property);
      const isLocked = readOnly || locked;
      input.disabled = isLocked || (!directEdit && !editing);
      const tip = readOnly
        ? "当前数据库只读，不可修改"
        : locked
          ? "原生 SQLite 已有字段由原表定义，不可修改（新添加字段可选择）"
          : directEdit ? "单击修改" : editing ? "编辑中" : "双击单元格进入编辑";
      input.title = tip;
      if (cell) {
        cell.classList.toggle("schema-locked", isLocked);
        cell.title = tip;
      }
    }
    for (const ctrl of row.querySelectorAll("[data-time-role]")) {
      const cell = ctrl.closest("td");
      ctrl.disabled = readOnly;
      const tip = readOnly
        ? "当前数据库只读，不可修改"
        : ctrl.dataset.timeRole === "preset" ? "选择时间显示格式" : "输入自定义时间格式（如 年-月-日 时:分:秒、时:分:秒、时:分）";
      ctrl.title = tip;
      if (cell) {
        cell.classList.toggle("schema-locked", readOnly);
        if (ctrl.dataset.timeRole === "preset") cell.title = tip;
      }
    }
    row.classList.toggle("schema-editing", editing);
  }
  updateRecordsToolbar();
}
const COLUMN_WIDTHS_STORAGE_KEY = "rtdb.grid.colWidths.v1";
let columnWidthsCache = null;
function loadColumnWidths() {
  if (columnWidthsCache) return columnWidthsCache;
  try { columnWidthsCache = JSON.parse(localStorage.getItem(COLUMN_WIDTHS_STORAGE_KEY) || "{}") || {}; } catch { columnWidthsCache = {}; }
  return columnWidthsCache;
}
function saveColumnWidth(key, width) {
  const map = loadColumnWidths();
  if (width == null) delete map[key];
  else {
    map[key] = width;
    const keys = Object.keys(map);
    if (keys.length > 200) delete map[keys[0]];
  }
  try { localStorage.setItem(COLUMN_WIDTHS_STORAGE_KEY, JSON.stringify(map)); } catch { /* 忽略本地存储异常 */ }
}
function filterSchemaFieldRows() {
  const keyword = (el("schema-field-filter")?.value || "").trim().toLowerCase();
  for (const row of el("schema-fields").querySelectorAll("tbody tr")) {
    if (!keyword) { row.hidden = false; continue; }
    const eng = (row.querySelector('[data-property="eng_name"]')?.value || "").toLowerCase();
    const chn = (row.querySelector('[data-property="chn_name"]')?.value || "").toLowerCase();
    row.hidden = !eng.includes(keyword) && !chn.includes(keyword);
  }
}
function bindSchemaInputKeyNav(input, row) {
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation();
      if (input.dataset.originalValue !== undefined) {
        input.value = input.dataset.originalValue;
        input.closest("td")?.classList.remove("schema-modified");
      }
      row.dataset.editing = "false"; updateSchemaLocks(); input.blur();
    } else if (event.key === "Enter") {
      event.preventDefault();
      const prop = input.dataset.property, nextRow = row.nextElementSibling;
      row.dataset.editing = "false"; updateSchemaLocks();
      if (nextRow && prop) {
        const nextInput = nextRow.querySelector(`[data-property="${prop}"]`);
        if (nextInput && nextInput.tagName === "INPUT" && !nextInput.readOnly && !isReadOnlyDatabase()) {
          nextRow.dataset.editing = "true"; updateSchemaLocks(); nextInput.disabled = false;
          nextInput.focus({ preventScroll: true }); if (nextInput.select) nextInput.select();
        }
      }
    }
  });
}
function createSchemaFieldRow(column) {
    const row = node("tr"); row.addEventListener("contextmenu", (event) => openSchemaContextMenu(event, row));
    // 保持文本格双击编辑、单击选中，同时允许 Tab 在同一编辑行内切换而不失焦锁死。
    row.addEventListener("focusout", (event) => {
      if (row.dataset.editing === "true" && !row.contains(event.relatedTarget)) {
        row.dataset.editing = "false";
        updateSchemaLocks();
      }
    });
    let previousKind = column.data_type;
    const displayType = normalizeSchemaDisplayType(column.disp_type, previousKind, column.disp_menu);
    const originalName = column.__original_name || (state.table && state.definition?.columns.some((item) => item.eng_name === column.eng_name) ? column.eng_name : "");
    if (originalName) row.dataset.originalName = originalName;
    let defaultCell = null;
    let menuCell = null;
    const initialMenuRaw = column.disp_menu ?? "";
    const initialMenuNormalized = displayType === TIME_DISPLAY_TYPE
      ? (String(initialMenuRaw).trim() || DEFAULT_TIME_FORMAT)
      : displayType === MENU_DISPLAY_TYPE
        ? serializeMenuEntries(parseMenuDict(initialMenuRaw))
        : displayType === CHOICE_DISPLAY_TYPE
          ? migrateLegacyDropdownMenu(initialMenuRaw, previousKind)
          : "";
    const initialDefaultRaw = column.default_value == null ? "" : cellText(column.default_value);
    const initialDefaultText = column.default_value == null ? "" : typeof column.default_value === "string" && !integerTypes.has(columnKind(column)) ? JSON.stringify(column.default_value) : cellText(column.default_value);

    const syncDisplayTypeOptions = (dataTypeValue, preferredDisplayType) => {
      const displayInput = row.querySelector('[data-property="disp_type"]');
      if (!displayInput) return;
      const allowed = schemaDisplayTypesFor(dataTypeValue);
      const nextValue = allowed.includes(preferredDisplayType) ? preferredDisplayType : schemaDefaultDisplayType(dataTypeValue);
      displayInput.replaceChildren();
      for (const kind of allowed) displayInput.append(new Option(kind, kind));
      displayInput.value = nextValue;
      displayInput.closest("td")?.classList.toggle("schema-modified", displayInput.value !== displayInput.dataset.originalValue);
    };

    const renderDefaultControl = (rawValue, isInitial = false) => {
      if (!defaultCell) return;
      const typeValue = row.querySelector('[data-property="data_type"]')?.value || column.data_type;
      const dispTypeValue = row.querySelector('[data-property="disp_type"]')?.value || displayType;
      const menuRaw = row.querySelector('[data-property="disp_menu"]')?.value ?? initialMenuNormalized;
      const kind = String(typeValue || "str").toLowerCase();
      const isIntBoolMode = dispTypeValue === BOOL_DISPLAY_TYPE && isIntegerSchemaType(kind);
      const isMenuMode = dispTypeValue === MENU_DISPLAY_TYPE && isIntegerSchemaType(kind);
      const isChoiceMode = dispTypeValue === CHOICE_DISPLAY_TYPE && isChoiceSchemaType(kind);
      const menuEntries = isIntBoolMode
        ? [{ value: "0", label: "0（否）" }, { value: "1", label: "1（是）" }]
        : isMenuMode
          ? parseMenuDict(menuRaw).map((item) => ({ value: item.key, label: item.value ? `${item.value} (${item.key})` : item.key }))
          : isChoiceMode
            ? parseChoiceSet(menuRaw, kind).map((item) => ({ value: item, label: item }))
            : [];
      const useSelect = boolTypes.has(kind) || menuEntries.length > 0;
      const existing = defaultCell.querySelector('[data-property="default_value"]');
      const originalValue = existing ? existing.dataset.originalValue : (useSelect ? initialDefaultRaw : initialDefaultText);
      const input = node(useSelect ? "select" : "input");
      input.dataset.property = "default_value";
      input.setAttribute("aria-label", `${column.eng_name || "新字段"} default_value`);
      let normalized = rawValue ?? "";
      if (useSelect && typeof normalized === "string" && normalized.startsWith('"') && normalized.endsWith('"')) {
        try { normalized = String(JSON.parse(normalized)); } catch { /* 保持原文本 */ }
      }
      if (useSelect) {
        input.append(new Option("未设置", ""));
        if (boolTypes.has(kind)) {
          input.append(new Option("true（是）", "true"), new Option("false（否）", "false"));
        } else {
          for (const item of menuEntries) input.append(new Option(item.label, item.value));
        }
        if (normalized !== "" && ![...input.options].some((option) => option.value === normalized)) {
          input.append(new Option(`${normalized}（当前）`, normalized));
        }
        input.value = normalized;
        defaultCell.title = "单击选择默认值";
      } else {
        input.value = isInitial ? initialDefaultText : normalized;
        input.placeholder = "未设置";
        defaultCell.title = '双击编辑默认值；留空不设置，空字符串填 ""，对象/数组填 JSON';
      }
      input.dataset.originalValue = originalValue;
      const onDefaultChange = () => {
        schemaChanged();
        defaultCell.classList.toggle("schema-modified", String(input.value) !== input.dataset.originalValue);
      };
      input.oninput = onDefaultChange; input.onchange = onDefaultChange;
      defaultCell.replaceChildren(input);
      defaultCell.classList.toggle("schema-modified", !isInitial && String(input.value) !== input.dataset.originalValue);
    };

    const renderMenuControl = (rawMenuValue, isInitial = false) => {
      if (!menuCell) return;
      const typeValue = row.querySelector('[data-property="data_type"]')?.value || column.data_type;
      const dispTypeValue = row.querySelector('[data-property="disp_type"]')?.value || displayType;
      const isTimeMode = dispTypeValue === TIME_DISPLAY_TYPE && isIntegerSchemaType(typeValue);
      const isMenuMode = dispTypeValue === MENU_DISPLAY_TYPE && isIntegerSchemaType(typeValue);
      const isChoiceMode = dispTypeValue === CHOICE_DISPLAY_TYPE && isChoiceSchemaType(typeValue);
      const existing = menuCell.querySelector('[data-property="disp_menu"]');
      const originalValue = existing ? existing.dataset.originalValue : initialMenuNormalized;
      if (isTimeMode) {
        const formatVal = String(rawMenuValue || "").trim() || DEFAULT_TIME_FORMAT;
        const isPreset = TIME_FORMAT_PRESETS.includes(formatVal);
        const wrap = node("div", null, `schema-time-format${isPreset ? "" : " is-custom"}`);
        const hidden = node("input");
        hidden.type = "hidden";
        hidden.dataset.property = "disp_menu";
        hidden.dataset.originalValue = originalValue;
        hidden.value = formatVal;

        const preset = node("select");
        preset.dataset.timeRole = "preset";
        preset.setAttribute("aria-label", `${column.eng_name || "新字段"} 时间格式`);
        for (const fmt of TIME_FORMAT_PRESETS) preset.append(new Option(fmt, fmt));
        preset.append(new Option("自定义…", "__custom__"));
        preset.value = isPreset ? formatVal : "__custom__";

        const custom = node("input");
        custom.type = "text";
        custom.dataset.timeRole = "custom";
        custom.placeholder = DEFAULT_TIME_FORMAT;
        custom.value = formatVal;
        custom.hidden = isPreset;
        custom.setAttribute("aria-label", `${column.eng_name || "新字段"} 自定义时间格式`);

        const commitTimeFormat = (nextFormat) => {
          hidden.value = nextFormat;
          schemaChanged();
          menuCell.classList.toggle("schema-modified", String(hidden.value) !== hidden.dataset.originalValue);
        };
        preset.onchange = () => {
          if (preset.value === "__custom__") {
            wrap.classList.add("is-custom");
            custom.hidden = false;
            custom.value = hidden.value || DEFAULT_TIME_FORMAT;
            custom.focus({ preventScroll: true });
            custom.select();
          } else {
            wrap.classList.remove("is-custom");
            custom.hidden = true;
            custom.value = preset.value;
            commitTimeFormat(preset.value);
          }
        };
        custom.oninput = () => commitTimeFormat(custom.value.trim() || DEFAULT_TIME_FORMAT);
        custom.onchange = () => commitTimeFormat(custom.value.trim() || DEFAULT_TIME_FORMAT);

        wrap.append(preset, custom, hidden);
        menuCell.replaceChildren(wrap);
        menuCell.classList.toggle("schema-modified", !isInitial && String(hidden.value) !== hidden.dataset.originalValue);
      } else if (isMenuMode) {
        const jsonVal = serializeMenuEntries(parseMenuDict(rawMenuValue));
        const input = node("input");
        input.type = "text";
        input.readOnly = true;
        input.className = "schema-menu-json";
        input.dataset.property = "disp_menu";
        input.dataset.originalValue = originalValue;
        input.value = jsonVal;
        input.placeholder = "双击配置菜单选项";
        input.title = "双击配置菜单选项";
        input.setAttribute("aria-label", `${column.eng_name || "新字段"} 菜单选项 JSON`);
        menuCell.replaceChildren(input);
        menuCell.title = "双击配置菜单选项";
        menuCell.classList.toggle("schema-modified", !isInitial && String(input.value) !== input.dataset.originalValue);
      } else if (isChoiceMode) {
        const jsonVal = serializeChoiceSet(parseChoiceSet(rawMenuValue, typeValue), typeValue);
        const input = node("input");
        input.type = "text";
        input.readOnly = true;
        input.className = "schema-choice-json";
        input.dataset.property = "disp_menu";
        input.dataset.originalValue = originalValue;
        input.value = jsonVal;
        input.placeholder = "双击配置选择选项";
        input.title = "双击配置选择选项";
        input.setAttribute("aria-label", `${column.eng_name || "新字段"} 选择选项 JSON`);
        menuCell.replaceChildren(input);
        menuCell.title = "双击配置选择选项";
        menuCell.classList.toggle("schema-modified", !isInitial && String(input.value) !== input.dataset.originalValue);
      } else {
        const input = node("input");
        input.type = "text";
        input.readOnly = true;
        input.disabled = true;
        input.className = "schema-menu-none";
        input.dataset.property = "disp_menu";
        input.dataset.originalValue = originalValue;
        input.value = "";
        input.placeholder = "—";
        input.setAttribute("aria-label", `${column.eng_name || "新字段"} disp_menu`);
        menuCell.replaceChildren(input);
        menuCell.title = "";
        menuCell.classList.toggle("schema-modified", !isInitial && String(input.value) !== input.dataset.originalValue);
      }
    };

    for (const property of ["eng_name", "chn_name", "data_type", "primary_key", "default_value", "disp_type", "disp_menu"]) {
      const cell = node("td");
      if (property === "default_value") {
        defaultCell = cell;
        cell.ondblclick = (event) => {
          event.preventDefault(); event.stopPropagation();
          const currentInput = cell.querySelector('[data-property="default_value"]');
          if (!currentInput || currentInput.tagName === "SELECT" || isReadOnlyDatabase() || isLockedNativeField(row, property)) return;
          row.dataset.editing = "true"; updateSchemaLocks(); currentInput.disabled = false; currentInput.focus({ preventScroll: true });
          if (currentInput.select) currentInput.select();
        };
        row.append(cell);
        continue;
      }
      if (property === "disp_menu") {
        menuCell = cell;
        cell.ondblclick = (event) => {
          event.preventDefault(); event.stopPropagation();
          if (isReadOnlyDatabase() || isLockedNativeField(row, property)) return;
          const menuJsonInput = cell.querySelector("input.schema-menu-json");
          if (menuJsonInput) {
            const fieldLabel = row.querySelector('[data-property="eng_name"]')?.value?.trim() || column.eng_name || "新字段";
            openMenuOptionsDialog({
              fieldName: fieldLabel,
              rawMenu: menuJsonInput.value,
              onApply: (nextJson) => {
                menuJsonInput.value = nextJson;
                schemaChanged();
                menuCell.classList.toggle("schema-modified", String(menuJsonInput.value) !== menuJsonInput.dataset.originalValue);
                const prevDefault = defaultCell?.querySelector('[data-property="default_value"]')?.value ?? "";
                renderDefaultControl(prevDefault, false);
                updateSchemaLocks();
              }
            });
            return;
          }
          const choiceJsonInput = cell.querySelector("input.schema-choice-json");
          if (choiceJsonInput) {
            const fieldLabel = row.querySelector('[data-property="eng_name"]')?.value?.trim() || column.eng_name || "新字段";
            const currentDataType = row.querySelector('[data-property="data_type"]')?.value || column.data_type;
            openChoiceOptionsDialog({
              fieldName: fieldLabel,
              dataType: currentDataType,
              rawMenu: choiceJsonInput.value,
              onApply: (nextJson) => {
                choiceJsonInput.value = nextJson;
                schemaChanged();
                menuCell.classList.toggle("schema-modified", String(choiceJsonInput.value) !== choiceJsonInput.dataset.originalValue);
                const prevDefault = defaultCell?.querySelector('[data-property="default_value"]')?.value ?? "";
                renderDefaultControl(prevDefault, false);
                updateSchemaLocks();
              }
            });
            return;
          }
          const currentInput = cell.querySelector('input[data-property="disp_menu"]:not([type="hidden"])');
          if (!currentInput || currentInput.classList.contains("schema-menu-none")) return;
          row.dataset.editing = "true"; updateSchemaLocks(); currentInput.disabled = false; currentInput.focus({ preventScroll: true });
          if (currentInput.select) currentInput.select();
        };
        row.append(cell);
        continue;
      }
      const input = node(["data_type", "disp_type"].includes(property) ? "select" : "input"); input.dataset.property = property;
      const directEdit = input.tagName === "SELECT" || ["checkbox", "radio"].includes(input.type);
      cell.title = directEdit ? "单击修改" : "双击此单元格编辑";
      input.setAttribute("aria-label", `${column.eng_name || "新字段"} ${property}`);
      if (property === "data_type") {
        const types = isNativeDatabase()
          ? ["int", "float", "str", "bool", "blob"]
          : ["int", "float", "str", "bool", "json", "list", "dict", "datetime"];
        for (const kind of new Set([...types, column.data_type])) input.append(new Option(kind, kind));
      }
      if (property === "disp_type") {
        for (const kind of schemaDisplayTypesFor(previousKind)) input.append(new Option(kind, kind));
      }
      if (property === "primary_key") { input.type = "checkbox"; input.checked = column[property]; }
      else input.value = property === "chn_name" ? chineseName(column) : property === "disp_type" ? displayType : column[property] ?? "";
      input.dataset.originalValue = input.type === "checkbox" ? String(input.checked) : input.value;
      const update = () => {
        schemaChanged();
        if (property === "data_type") {
          const displayInput = row.querySelector('[data-property="disp_type"]');
          const prevDisplay = displayInput?.value || displayType;
          const nextPreferred = prevDisplay === schemaDefaultDisplayType(previousKind)
            ? schemaDefaultDisplayType(input.value)
            : prevDisplay;
          const wasTime = prevDisplay === TIME_DISPLAY_TYPE;
          const wasMenu = prevDisplay === MENU_DISPLAY_TYPE;
          const wasChoice = prevDisplay === CHOICE_DISPLAY_TYPE;
          syncDisplayTypeOptions(input.value, nextPreferred);
          previousKind = input.value;
          const nowTime = displayInput?.value === TIME_DISPLAY_TYPE;
          const nowMenu = displayInput?.value === MENU_DISPLAY_TYPE;
          const nowChoice = displayInput?.value === CHOICE_DISPLAY_TYPE;
          const currentMenuVal = menuCell?.querySelector('[data-property="disp_menu"]')?.value ?? "";
          let nextMenuVal = "";
          if (nowTime) nextMenuVal = wasTime && String(currentMenuVal).trim() ? currentMenuVal : DEFAULT_TIME_FORMAT;
          else if (nowMenu) nextMenuVal = wasMenu ? serializeMenuEntries(parseMenuDict(currentMenuVal)) : "";
          else if (nowChoice) nextMenuVal = serializeChoiceSet(parseChoiceSet(currentMenuVal, input.value), input.value);
          renderMenuControl(nextMenuVal, false);
          const prevDefault = defaultCell?.querySelector('[data-property="default_value"]')?.value ?? "";
          renderDefaultControl(prevDefault, false);
          updateSchemaLocks();
        } else if (property === "disp_type") {
          const currentDataType = row.querySelector('[data-property="data_type"]')?.value || column.data_type;
          const currentMenuVal = menuCell?.querySelector('[data-property="disp_menu"]')?.value ?? "";
          const trimmedMenu = String(currentMenuVal).trim();
          let nextMenuVal = "";
          if (input.value === TIME_DISPLAY_TYPE) {
            nextMenuVal = (!trimmedMenu || trimmedMenu.includes("|") || trimmedMenu.startsWith("{") || trimmedMenu.startsWith("["))
              ? DEFAULT_TIME_FORMAT : currentMenuVal;
          } else if (input.value === MENU_DISPLAY_TYPE) {
            nextMenuVal = serializeMenuEntries(parseMenuDict(currentMenuVal));
          } else if (input.value === CHOICE_DISPLAY_TYPE) {
            nextMenuVal = serializeChoiceSet(parseChoiceSet(currentMenuVal, currentDataType), currentDataType);
          }
          renderMenuControl(nextMenuVal, false);
          const prevDefault = defaultCell?.querySelector('[data-property="default_value"]')?.value ?? "";
          renderDefaultControl(prevDefault, false);
          updateSchemaLocks();
        }
        const current = input.type === "checkbox" ? String(input.checked) : String(input.value);
        cell.classList.toggle("schema-modified", current !== input.dataset.originalValue);
      };
      input.oninput = update; input.onchange = update;
      if (!directEdit) bindSchemaInputKeyNav(input, row);
      cell.ondblclick = (event) => {
        event.preventDefault(); event.stopPropagation();
        if (isReadOnlyDatabase() || isLockedNativeField(row, property)) return;
        row.dataset.editing = "true"; updateSchemaLocks(); input.disabled = false; input.focus({ preventScroll: true });
        if (input.select && input.type !== "checkbox") input.select();
      };
      cell.append(input); row.append(cell);
    }
    const actCell = node("td");
    const actBar = node("div", null, "schema-row-actions");
    const moveUp = node("button", "↑", "ghost"); moveUp.type = "button"; moveUp.title = "上移字段"; moveUp.setAttribute("aria-label", "上移字段");
    moveUp.onclick = () => { if (!isReadOnlyDatabase() && row.previousElementSibling) { row.parentElement.insertBefore(row, row.previousElementSibling); schemaChanged(); } };
    const moveDown = node("button", "↓", "ghost"); moveDown.type = "button"; moveDown.title = "下移字段"; moveDown.setAttribute("aria-label", "下移字段");
    moveDown.onclick = () => { if (!isReadOnlyDatabase() && row.nextElementSibling) { row.parentElement.insertBefore(row.nextElementSibling, row); schemaChanged(); } };
    const delBtn = node("button", "×", "ghost is-delete"); delBtn.type = "button"; delBtn.title = "移除字段"; delBtn.setAttribute("aria-label", "移除字段");
    delBtn.onclick = () => { if (!isReadOnlyDatabase()) { row.remove(); schemaChanged(); notice("字段已移除，保存结构后正式生效"); } };
    actBar.append(moveUp, moveDown, delBtn);
    actCell.append(actBar);
    row.append(actCell);
    renderMenuControl(initialMenuNormalized, true);
    renderDefaultControl(initialDefaultRaw, true);
    return row;
}
function renderSchema(definition) {
  state.schemaRendered = true;
  const originalIndexNames=Object.fromEntries(definition.columns.map(column=>[column.eng_name,column.__original_name||column.eng_name]));
  state.schemaIndexes=definition.indexes==null?null:definition.indexes.map(fields=>fields.map(name=>originalIndexNames[name]||name));
  el("schema-name").value = definition.head.eng_name; el("schema-label").value = chineseName(definition.head); el("schema-name").disabled = false;
  if (el("schema-field-filter")) el("schema-field-filter").value = "";
  const grid = node("table"), head = node("tr"), thead = node("thead"), body = node("tbody");
  for (const label of ["英文名", "中文名", "类型", "主键", "默认值", "显示类型", "选项 / 格式", "操作"]) head.append(node("th", label));
  thead.append(head); grid.append(thead);
  for (const column of definition.columns) body.append(createSchemaFieldRow(column));
  grid.append(body); el("schema-fields").replaceChildren(grid); updateSchemaLocks();
}

// 菜单类型选项表格弹窗（支持 Key / Value 增删改与 JSON dict 校验序列化）
let menuOptionsDialogState = null;
function renderMenuOptionsEmptyRow(tbody) {
  if (tbody.querySelector("tr[data-menu-row]")) return;
  const tr = node("tr", null, "menu-options-empty-row");
  const td = node("td", "暂无菜单项，点击“添加选项”新增", "menu-options-empty");
  td.colSpan = 3;
  tr.append(td);
  tbody.append(tr);
}
function appendMenuOptionRow(tbody, keyVal = "", valueText = "", focusTarget = null) {
  tbody.querySelector(".menu-options-empty-row")?.remove();
  const tr = node("tr");
  tr.dataset.menuRow = "1";
  const keyTd = node("td"), valTd = node("td"), actTd = node("td");
  const keyInput = node("input");
  keyInput.type = "text";
  keyInput.inputMode = "numeric";
  keyInput.dataset.menuCol = "key";
  keyInput.placeholder = "0";
  keyInput.value = keyVal;
  keyInput.setAttribute("aria-label", "Key（整数）");
  const valInput = node("input");
  valInput.type = "text";
  valInput.dataset.menuCol = "value";
  valInput.placeholder = "显示名称";
  valInput.value = valueText;
  valInput.setAttribute("aria-label", "Value（显示值）");
  const delBtn = node("button", "删除", "danger-outline small");
  delBtn.type = "button";
  delBtn.onclick = () => {
    tr.remove();
    renderMenuOptionsEmptyRow(tbody);
    el("menu-options-error").textContent = "";
  };
  const onKeyNav = (col) => (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault(); el("menu-options-apply").click();
      return;
    }
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (col === "key") { valInput.focus({ preventScroll: true }); valInput.select(); return; }
    const nextRow = tr.nextElementSibling;
    const nextKey = nextRow?.querySelector('input[data-menu-col="key"]');
    if (nextKey) { nextKey.focus({ preventScroll: true }); nextKey.select(); }
    else appendMenuOptionRow(tbody, nextMenuIntegerKey(tbody), "", "value");
  };
  keyInput.onkeydown = onKeyNav("key");
  valInput.onkeydown = onKeyNav("value");
  keyTd.append(keyInput);
  valTd.append(valInput);
  actTd.append(delBtn);
  tr.append(keyTd, valTd, actTd);
  tbody.append(tr);
  if (focusTarget === "value") { valInput.focus({ preventScroll: true }); valInput.select(); }
  else if (focusTarget === "key") { keyInput.focus({ preventScroll: true }); keyInput.select(); }
}
function nextMenuIntegerKey(tbody) {
  const keys = [...tbody.querySelectorAll('input[data-menu-col="key"]')]
    .map((input) => input.value.trim())
    .filter((text) => /^-?\d+$/.test(text))
    .map((text) => BigInt(text));
  if (!keys.length) return "0";
  let maxKey = keys[0];
  for (const k of keys) if (k > maxKey) maxKey = k;
  return (maxKey + 1n).toString();
}
function openMenuOptionsDialog({ fieldName, rawMenu, onApply }) {
  const dialog = el("menu-options-dialog");
  const tbody = el("menu-options-body");
  if (!dialog || !tbody) return;
  menuOptionsDialogState = { onApply };
  el("menu-options-title").textContent = fieldName ? `菜单选项配置 · ${fieldName}` : "菜单选项配置";
  el("menu-options-error").textContent = "";
  tbody.replaceChildren();
  const entries = parseMenuDict(rawMenu);
  if (entries.length) {
    for (const item of entries) appendMenuOptionRow(tbody, item.key, item.value);
  } else {
    renderMenuOptionsEmptyRow(tbody);
  }
  dialog.showModal();
  const firstInput = tbody.querySelector("input");
  if (firstInput) firstInput.focus({ preventScroll: true });
  else el("menu-options-add")?.focus({ preventScroll: true });
}
if (el("menu-options-add")) {
  el("menu-options-add").onclick = () => {
    const tbody = el("menu-options-body");
    el("menu-options-error").textContent = "";
    appendMenuOptionRow(tbody, nextMenuIntegerKey(tbody), "", "value");
  };
}
if (el("menu-options-cancel")) el("menu-options-cancel").onclick = () => el("menu-options-dialog")?.close();
if (el("menu-options-close-x")) el("menu-options-close-x").onclick = () => el("menu-options-dialog")?.close();
if (el("menu-options-apply")) {
  el("menu-options-apply").onclick = () => {
    const tbody = el("menu-options-body"), err = el("menu-options-error");
    err.textContent = "";
    const rows = [...tbody.querySelectorAll("tr[data-menu-row]")];
    const entries = [];
    const seen = new Set();
    for (let i = 0; i < rows.length; i++) {
      const keyInput = rows[i].querySelector('input[data-menu-col="key"]');
      const valInput = rows[i].querySelector('input[data-menu-col="value"]');
      const key = keyInput.value.trim();
      const val = valInput.value.trim();
      if (!key && !val) continue;
      if (!/^-?\d+$/.test(key)) {
        err.textContent = `第 ${i + 1} 行 Key 必须是整数`;
        keyInput.focus({ preventScroll: true });
        return;
      }
      try {
        const big = BigInt(key);
        if (big < -(2n ** 63n) || big >= 2n ** 63n) throw new Error("range");
      } catch {
        err.textContent = `第 ${i + 1} 行 Key 超出 64 位整数范围`;
        keyInput.focus({ preventScroll: true });
        return;
      }
      const normKey = BigInt(key).toString();
      if (seen.has(normKey)) {
        err.textContent = `Key "${normKey}" 重复，请保持唯一`;
        keyInput.focus({ preventScroll: true });
        return;
      }
      seen.add(normKey);
      entries.push({ key: normKey, value: val });
    }
    const jsonText = serializeMenuEntries(entries);
    if (menuOptionsDialogState?.onApply) menuOptionsDialogState.onApply(jsonText);
    el("menu-options-dialog")?.close();
  };
}

// 选择类型选项表格弹窗（按字段类型严格校验整数/字符串且禁止重复，按 JSON 数组集合存储）
let choiceOptionsDialogState = null;
function renderChoiceOptionsEmptyRow(tbody) {
  if (tbody.querySelector("tr[data-choice-row]")) return;
  const tr = node("tr", null, "menu-options-empty-row");
  const td = node("td", "暂无待选项，点击“添加选项”新增", "menu-options-empty");
  td.colSpan = 2;
  tr.append(td);
  tbody.append(tr);
}
function appendChoiceOptionRow(tbody, val = "", isInt = false, autoFocus = false) {
  tbody.querySelector(".menu-options-empty-row")?.remove();
  const tr = node("tr");
  tr.dataset.choiceRow = "1";
  const valTd = node("td"), actTd = node("td");
  const valInput = node("input");
  valInput.type = "text";
  if (isInt) {
    valInput.inputMode = "numeric";
    valInput.dataset.choiceInt = "1";
    valInput.placeholder = "0";
  } else {
    valInput.placeholder = "输入选项文本";
  }
  valInput.dataset.choiceCol = "value";
  valInput.value = val;
  valInput.setAttribute("aria-label", isInt ? "待选择值（整数）" : "待选择值（文本）");
  valInput.onkeydown = (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault(); el("choice-options-apply").click();
      return;
    }
    if (event.key !== "Enter") return;
    event.preventDefault();
    const nextInput = tr.nextElementSibling?.querySelector('input[data-choice-col="value"]');
    if (nextInput) { nextInput.focus({ preventScroll: true }); nextInput.select(); }
    else appendChoiceOptionRow(tbody, isInt ? nextChoiceIntegerValue(tbody) : "", isInt, true);
  };
  const delBtn = node("button", "删除", "danger-outline small");
  delBtn.type = "button";
  delBtn.onclick = () => {
    tr.remove();
    renderChoiceOptionsEmptyRow(tbody);
    el("choice-options-error").textContent = "";
  };
  valTd.append(valInput);
  actTd.append(delBtn);
  tr.append(valTd, actTd);
  tbody.append(tr);
  if (autoFocus) { valInput.focus({ preventScroll: true }); valInput.select(); }
}
function nextChoiceIntegerValue(tbody) {
  const vals = [...tbody.querySelectorAll('input[data-choice-col="value"]')]
    .map((input) => input.value.trim())
    .filter((text) => /^-?\d+$/.test(text))
    .map((text) => BigInt(text));
  if (!vals.length) return "0";
  let maxVal = vals[0];
  for (const v of vals) if (v > maxVal) maxVal = v;
  return (maxVal + 1n).toString();
}
function openChoiceOptionsDialog({ fieldName, dataType, rawMenu, onApply }) {
  const dialog = el("choice-options-dialog");
  const tbody = el("choice-options-body");
  if (!dialog || !tbody) return;
  const isInt = isIntegerSchemaType(dataType);
  choiceOptionsDialogState = { dataType, isInt, onApply };
  el("choice-options-title").textContent = fieldName ? `选择选项配置 · ${fieldName}` : "选择选项配置";
  el("choice-options-value-head").textContent = isInt ? "待选择值（整数）" : "待选择值（文本）";
  el("choice-options-error").textContent = "";
  tbody.replaceChildren();
  const items = parseChoiceSet(rawMenu, dataType);
  if (items.length) {
    for (const item of items) appendChoiceOptionRow(tbody, item, isInt, false);
  } else {
    renderChoiceOptionsEmptyRow(tbody);
  }
  dialog.showModal();
  const firstInput = tbody.querySelector("input");
  if (firstInput) firstInput.focus({ preventScroll: true });
  else el("choice-options-add")?.focus({ preventScroll: true });
}
if (el("choice-options-add")) {
  el("choice-options-add").onclick = () => {
    const tbody = el("choice-options-body");
    const isInt = Boolean(choiceOptionsDialogState?.isInt);
    el("choice-options-error").textContent = "";
    appendChoiceOptionRow(tbody, isInt ? nextChoiceIntegerValue(tbody) : "", isInt, true);
  };
}
if (el("choice-options-cancel")) el("choice-options-cancel").onclick = () => el("choice-options-dialog")?.close();
if (el("choice-options-close-x")) el("choice-options-close-x").onclick = () => el("choice-options-dialog")?.close();
if (el("choice-options-apply")) {
  el("choice-options-apply").onclick = () => {
    const tbody = el("choice-options-body"), err = el("choice-options-error");
    const isInt = Boolean(choiceOptionsDialogState?.isInt);
    err.textContent = "";
    const rows = [...tbody.querySelectorAll("tr[data-choice-row]")];
    const values = [];
    const seen = new Set();
    for (let i = 0; i < rows.length; i++) {
      const input = rows[i].querySelector('input[data-choice-col="value"]');
      const raw = input.value;
      if (!raw.trim()) continue;
      if (isInt) {
        const trimmed = raw.trim();
        if (!/^-?\d+$/.test(trimmed)) {
          err.textContent = `第 ${i + 1} 行只能输入整数`;
          input.focus({ preventScroll: true });
          return;
        }
        try {
          const big = BigInt(trimmed);
          if (big < -(2n ** 63n) || big >= 2n ** 63n) throw new Error("range");
        } catch {
          err.textContent = `第 ${i + 1} 行整数超出 64 位范围`;
          input.focus({ preventScroll: true });
          return;
        }
        const norm = BigInt(trimmed).toString();
        if (seen.has(norm)) {
          err.textContent = `选项 "${norm}" 重复，不能重复`;
          input.focus({ preventScroll: true });
          return;
        }
        seen.add(norm);
        values.push(norm);
      } else {
        const strVal = raw.trim();
        if (seen.has(strVal)) {
          err.textContent = `选项 "${strVal}" 重复，不能重复`;
          input.focus({ preventScroll: true });
          return;
        }
        seen.add(strVal);
        values.push(strVal);
      }
    }
    const jsonText = serializeChoiceSet(values, choiceOptionsDialogState?.dataType || "str");
    if (choiceOptionsDialogState?.onApply) choiceOptionsDialogState.onApply(jsonText);
    el("choice-options-dialog")?.close();
  };
}

// 时间类型单元格修改窗（可视化日期/时间/毫秒选择 + 自动识别存储单位 + 实时预览）
let timeEditDialogState = null;
function refreshTimeEditPreview() {
  const dtInput = el("time-edit-datetime"), msInput = el("time-edit-ms"), unitSelect = el("time-edit-unit");
  const displayEl = el("time-edit-preview-display"), rawEl = el("time-edit-preview-raw");
  const msLabel = el("time-edit-ms-label");
  if (!dtInput || !msInput || !unitSelect) return;
  const unit = unitSelect.value || "ms";
  const hideMs = unit === "s";
  msInput.disabled = hideMs;
  if (msLabel) msLabel.style.opacity = hideMs ? "0.48" : "";
  if (!dtInput.value) {
    displayEl.textContent = "NULL";
    rawEl.textContent = "NULL";
    return;
  }
  const subMs = timeEditDialogState?.unit === unit ? (timeEditDialogState.subMs || 0n) : 0n;
  const ts = datetimeLocalToTimestamp(dtInput.value, msInput.value, unit, subMs);
  if (ts == null) {
    displayEl.textContent = "—";
    rawEl.textContent = "—";
    return;
  }
  const fmt = timeEditDialogState?.column?.disp_menu || DEFAULT_TIME_FORMAT;
  displayEl.textContent = formatTimestampValue(ts, fmt) || ts;
  rawEl.textContent = ts;
}
function openTimeEditDialog(record, column, cell) {
  const dialog = el("time-edit-dialog");
  if (!dialog) return;
  const pending = pendingCell(record, column.eng_name);
  const raw = pending?.raw ?? cellText(record.row[column.eng_name]);
  const unit = detectTimestampUnit(raw, column);
  const subMs = extractSubMsOffset(raw, unit);
  const date = timestampToDate(raw);
  timeEditDialogState = { record, column, cell, unit, subMs };
  const label = el("chinese-columns")?.checked ? chineseName(column) : column.eng_name;
  el("time-edit-title").textContent = `修改时间 · ${label}`;
  el("time-edit-error").textContent = "";
  el("time-edit-unit").value = unit;
  if (date) {
    el("time-edit-datetime").value = dateToDatetimeLocalValue(date);
    el("time-edit-ms").value = String(date.getMilliseconds());
  } else {
    el("time-edit-datetime").value = "";
    el("time-edit-ms").value = "0";
  }
  el("time-edit-clear").hidden = Boolean(column.primary_key);
  refreshTimeEditPreview();
  dialog.showModal();
  el("time-edit-datetime").focus({ preventScroll: true });
}
function commitTimeEditValue(nextRaw) {
  if (!timeEditDialogState) return;
  const { record, column, cell } = timeEditDialogState;
  const liveCell = gridCells.get(record)?.get(column.eng_name) || (cell?.isConnected ? cell : null);
  const history = editSnapshot();
  setPendingCell(record, column, nextRaw);
  rememberEdit(history);
  if (liveCell) renderGridCellValue(record, column, liveCell);
  scheduleChartDraw();
  el("time-edit-dialog")?.close();
}
if (el("time-edit-dialog")) {
  el("time-edit-dialog").addEventListener("close", () => {
    const target = timeEditDialogState
      ? (gridCells.get(timeEditDialogState.record)?.get(timeEditDialogState.column.eng_name) || timeEditDialogState.cell)
      : null;
    if (target?.isConnected) target.focus({ preventScroll: true });
  });
}
if (el("time-edit-datetime")) {
  el("time-edit-datetime").oninput = refreshTimeEditPreview;
  el("time-edit-datetime").onchange = refreshTimeEditPreview;
  el("time-edit-datetime").onkeydown = (event) => {
    if (event.key === "Enter") { event.preventDefault(); el("time-edit-apply").click(); }
  };
}
if (el("time-edit-ms")) {
  el("time-edit-ms").oninput = refreshTimeEditPreview;
  el("time-edit-ms").onchange = refreshTimeEditPreview;
  el("time-edit-ms").onkeydown = (event) => {
    if (event.key === "Enter") { event.preventDefault(); el("time-edit-apply").click(); }
  };
}
if (el("time-edit-unit")) el("time-edit-unit").onchange = refreshTimeEditPreview;
if (el("time-edit-now")) {
  el("time-edit-now").onclick = () => {
    const now = new Date();
    el("time-edit-datetime").value = dateToDatetimeLocalValue(now);
    el("time-edit-ms").value = String(now.getMilliseconds());
    el("time-edit-error").textContent = "";
    refreshTimeEditPreview();
  };
}
if (el("time-edit-clear")) {
  el("time-edit-clear").onclick = () => {
    el("time-edit-datetime").value = "";
    el("time-edit-ms").value = "0";
    el("time-edit-error").textContent = "";
    refreshTimeEditPreview();
  };
}
if (el("time-edit-cancel")) el("time-edit-cancel").onclick = () => { el("time-edit-dialog")?.close(); timeEditDialogState?.cell?.focus({ preventScroll: true }); };
if (el("time-edit-close-x")) el("time-edit-close-x").onclick = () => { el("time-edit-dialog")?.close(); timeEditDialogState?.cell?.focus({ preventScroll: true }); };
if (el("time-edit-apply")) {
  el("time-edit-apply").onclick = () => {
    const err = el("time-edit-error");
    err.textContent = "";
    const dtVal = el("time-edit-datetime").value.trim();
    if (!dtVal) {
      if (timeEditDialogState?.column?.primary_key) {
        err.textContent = "主键时间字段不能为空";
        el("time-edit-datetime").focus({ preventScroll: true });
        return;
      }
      commitTimeEditValue("");
      return;
    }
    const msRaw = el("time-edit-ms").value.trim();
    const unit = el("time-edit-unit").value || "ms";
    if (unit !== "s" && msRaw !== "") {
      const msNum = Number(msRaw);
      if (!Number.isInteger(msNum) || msNum < 0 || msNum > 999) {
        err.textContent = "毫秒必须是 0–999 的整数";
        el("time-edit-ms").focus({ preventScroll: true });
        return;
      }
    }
    const subMs = timeEditDialogState?.unit === unit ? (timeEditDialogState.subMs || 0n) : 0n;
    const ts = datetimeLocalToTimestamp(dtVal, msRaw, unit, subMs);
    if (ts == null) {
      err.textContent = "请输入有效的日期与时间";
      el("time-edit-datetime").focus({ preventScroll: true });
      return;
    }
    commitTimeEditValue(ts);
  };
}
function closeSchemaContextMenu(restoreFocus = false) {
  const menu = el("schema-context-menu"), row = state.schemaContextRow;
  menu.hidden = true; state.schemaContextRow = null;
  if (restoreFocus) (row?.isConnected ? row : el("schema-fields")).focus({ preventScroll: true });
}
function openSchemaContextMenu(event, row) {
  event.preventDefault(); event.stopPropagation();
  if (state.busy) return;
  closeAllContextMenus("schema");
  state.schemaContextRow = row;
  const menu = el("schema-context-menu"); menu.hidden = false; menu.scrollTop = 0;
  const indexes = menu.querySelector('[data-schema-action="indexes"]');
  indexes.disabled = isReadOnlyDatabase() || isNativeDatabase();
  indexes.title = isNativeDatabase() ? "原生 SQLite 的索引由原表定义" : "";
  menu.querySelector('[data-schema-action="remove-column"]').disabled = isReadOnlyDatabase();
  const margin = 8, width = menu.offsetWidth, height = menu.offsetHeight;
  menu.style.left = `${Math.max(margin, Math.min(event.clientX, window.innerWidth - width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(event.clientY, window.innerHeight - height - margin))}px`;
  menu.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
}
function handleSchemaContextAction(actionName) {
  const row = state.schemaContextRow; closeSchemaContextMenu();
  if(actionName==='indexes'){void run(()=>schemaTools.openIndexes());return;}
  if(actionName==='preview'){void run(()=>schemaTools.preview());return;}
  if(actionName==='query-plan'){void run(()=>schemaTools.queryPlan());return;}
  if (!row || actionName !== "remove-column") return;
  row.remove(); schemaChanged(); notice("字段已移除，保存结构后正式生效");
}
function closeSidebarContextMenu(restoreFocus = false) {
  const menu = el("sidebar-context-menu"), trigger = state.sidebarContextTrigger;
  closeContextSubmenus(menu);
  menu.hidden = true; state.sidebarContextTrigger = null; state.sidebarContextScope = null;
  if (restoreFocus) (trigger?.isConnected ? trigger : el("table-list")).focus({ preventScroll: true });
}
function openSidebarContextMenu(event, scope = "tables") {
  event.preventDefault(); event.stopPropagation();
  if (state.busy) return;
  closeAllContextMenus("sidebar");
  state.sidebarContextScope = scope;
  state.sidebarContextTrigger = event.target.closest("[tabindex]") || el(scope === "fields" ? "field-list" : "table-list");
  const menu = el("sidebar-context-menu");
  menu.setAttribute("aria-label", scope === "fields" ? "字段操作" : "表索引操作");
  menu.querySelectorAll("[data-sidebar-scope]").forEach((button) => {
    button.hidden = button.dataset.sidebarScope !== scope;
  });
  const hasFields = state.resultColumns.length > 0;
  for (const name of ["select-all-fields", "invert-fields", "clear-fields"]) {
    menu.querySelector(`[data-sidebar-action="${name}"]`).disabled = !hasFields;
  }
  menu.querySelector('[data-sidebar-action="backup"]').disabled = !state.checkedTables.size;
  const unavailable = !state.database || isReadOnlyDatabase();
  for (const name of ["load-definition", "restore"]) menu.querySelector(`[data-sidebar-action="${name}"]`).disabled = unavailable || isNativeDatabase();
  menu.querySelector('[data-sidebar-action="receipt-maintenance"]').disabled = unavailable;
  menu.querySelector('[data-sidebar-action="csv-import"]').disabled = unavailable || !state.table;
  syncContextSubmenuGroups(menu);
  menu.hidden = false; menu.scrollTop = 0;
  const margin = 8, width = menu.offsetWidth, height = menu.offsetHeight;
  menu.style.left = `${Math.max(margin, Math.min(event.clientX, window.innerWidth - width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(event.clientY, window.innerHeight - height - margin))}px`;
  focusFirstContextItem(menu);
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
  else if (actionName === "receipt-maintenance") void run(()=>receiptMaintenance.open());
  else if (actionName === "storage-maintenance") void run(()=>storageMaintenance.open());
  else if (actionName === "database-snapshots") void run(()=>databaseSnapshots.open());
  else if (actionName === "csv-import") void run(()=>csvImports.open());
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
function sqlHistoryKey() { return `rtdb.sql-history:${JSON.stringify([state.historyScope, state.database, state.table])}`; }
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
function clearSqlHistory() {
  state.history = [];
  try { localStorage.removeItem(sqlHistoryKey()); } catch { /* Optional storage cleanup. */ }
  closeSqlHistory();
  notice("已清空当前表 SQL 历史");
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
  if (state.history.length) {
    const footer = node("div", null, "sql-history-footer");
    const clearBtn = action("清空历史", clearSqlHistory, "secondary small");
    clearBtn.onpointerdown = (event) => event.preventDefault();
    footer.append(clearBtn);
    list.append(footer);
  }
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
  const palette = currentChartPalette(), color = palette[index % palette.length], saved = state.chartStyles.get(key) || {};
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
  const columnMap = new Map(state.resultColumns.map((column) => [column.eng_name, column]));
  const series = state.chartSeries.map((item, index) => {
    const field = columnMap.get(item.field);
    const style = chartStyle(item.key, index);
    return { key: item.key, name: style.name || (field ? chartColumnLabel(field) : item.name), style, axis: state.chartAxes.get(item.key) || "left", color: style.lineColor, mode: item.mode, points: preferFloatChartPoints(item.points.map((point) => {
      const column = columnMap.get(point.field);
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
  const signature = JSON.stringify([state.chartPinned, model.series.map((series) =>
    [series.key, series.name, series.axis, series.style?.lineColor || series.color, state.chartHidden.has(series.key)])]);
  if (signature === legendSignature && target.childElementCount === model.series.length) return;
  legendSignature = signature;
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
  const theme = chartThemeTokens();
  const titleFill = isDarkTheme() && settings.color.toLowerCase() === chartTitleDefaults.color.toLowerCase() ? theme.titleColor : settings.color;
  context.save();
  context.font = `${settings.fontStyle.includes("italic") ? "italic " : ""}${settings.fontStyle.includes("bold") ? "bold " : ""}${settings.fontSize}px "${settings.fontFamily}", "Microsoft YaHei", sans-serif`;
  context.fillStyle = titleFill; context.textAlign = align; context.textBaseline = "middle";
  const metrics = context.measureText(state.chartTitle), availableWidth = Math.max(1, width - 24);
  const scale = Math.min(1, availableWidth / Math.max(1, metrics.width, metrics.actualBoundingBoxLeft + metrics.actualBoundingBoxRight));
  const anchor = align === "left" ? 12 : align === "right" ? width - 12 : width / 2;
  const x = Math.max(12 + metrics.actualBoundingBoxLeft * scale, Math.min(anchor, width - 12 - metrics.actualBoundingBoxRight * scale));
  context.fillText(state.chartTitle, x, vertical === "top" ? 4 + lineHeight / 2 : height - 4 - lineHeight / 2, Math.max(1, metrics.width * scale));
  context.restore();
}
function drawChart() {
  if (chartDrawFrame !== null) cancelAnimationFrame(chartDrawFrame);
  chartDrawFrame = null;
  if (!el("tab-chart").classList.contains("active") || document.documentElement.classList.contains("chart-collapsed")) return;
  const canvas = el("chart"), context = canvas.getContext("2d"), model = buildChartModel();
  for (const control of [el("chart-mode").closest("label"), el("chart-x").closest("label"), el("chart-fields")]) control.hidden = state.chartPinned;
  el("follow-chart-selection").hidden = el("chart-series-summary").hidden = !state.chartPinned;
  el("chart-series-summary").textContent = t("chart.seriesSummary", {
    count: model.series.length,
    ordinal: model.ordinal ? t("chart.ordinalHint") : ""
  });
  el("chart-series-summary").title = el("chart-series-summary").textContent;
  renderChartLegend(model);
  const visibleSeries = model.series.filter((series) => !state.chartHidden.has(series.key));
  const values = visibleSeries.flatMap((series) => series.points.map((point) => point.value)).filter(Number.isFinite);
  const available = visibleSeries.length && model.labels.length && values.length;
  const emptyBox = el("chart-empty");
  emptyBox.replaceChildren();
  emptyBox.append(node("span", model.series.length && !visibleSeries.length ? t("chart.allHidden") : t("chart.empty")));
  if (!available && !model.series.length && state.records.length && numericChartColumns().length) {
    const quickBtn = action(t("chart.quickPlot"), () => {
      const cols = numericChartColumns().slice(0, 4);
      if (!cols.length) return;
      state.chartPinned = false;
      state.chartCleared = false;
      state.chartMode = "columns";
      el("chart-mode").value = "columns";
      state.chartFields = new Set(cols.map((c) => c.eng_name));
      state.selected = new Set(state.records.map((_r, idx) => idx));
      state.selectedColumns.clear(); state.selectedCells.clear();
      paintGridSelection();
      drawChart();
    }, "secondary small chart-quick-plot");
    emptyBox.append(node("br"), quickBtn);
  }
  emptyBox.hidden = Boolean(available); canvas.hidden = !available;
  if (!available) { state.chartFrame = null; state.chartHits = []; return; }
  // Keep the chosen font size and a usable plot when a large title needs more height.
  if (state.chartTitle && canvas.clientHeight < chartTitleSpace() + 136) chartLayout.expand();
  const width = canvas.clientWidth, height = canvas.clientHeight, ratio = devicePixelRatio || 1;
  if (!width || !height) return;
  const pixelWidth = Math.round(width * ratio), pixelHeight = Math.round(height * ratio);
  if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
  if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
  const theme = chartThemeTokens(), themeName = currentTheme();
  const axisFont = '11px "JetBrains Mono", "Consolas", "Microsoft YaHei", monospace';
  const uiFont = '11px "Inter", "PingFang SC", "Microsoft YaHei", sans-serif';
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.font = uiFont;
  const titleSpace = chartTitleSpace();
  const titleAtTop = state.chartTitleSettings.position.startsWith("top-");
  const left = 68, right = 68, top = 30 + (titleAtTop ? titleSpace : 0), bottom = 46 + (titleAtTop ? 0 : titleSpace), plotWidth = Math.max(1, width - left - right), plotHeight = Math.max(1, height - top - bottom);
  const ranges = state.chartDrag?.ranges || { left: chartScale(visibleSeries.filter((series) => series.axis === "left").flatMap((series) => series.points.map((point) => point.value)), state.chartAxisSettings.left), right: chartScale(visibleSeries.filter((series) => series.axis === "right").flatMap((series) => series.points.map((point) => point.value)), state.chartAxisSettings.right) };
  if (!ranges.left && ranges.right) ranges.left = ranges.right;
  if (!ranges.right && ranges.left) ranges.right = ranges.left;
  const pointX = (index) => left + index / Math.max(1, model.labels.length - 1) * plotWidth;
  const pointY = (value, axis) => chartY(value, ranges[axis] || ranges.left, top, plotHeight);
  state.chartFrame = { model: { ...model, series: visibleSeries }, labels: model.labels, left, right, top, bottom, plotWidth, plotHeight, width, height, ranges, xAt: pointX, yAt: pointY };
  // 静态底图层（坐标轴、虚线水平网格、折线、数据点、标题）离屏缓存，游标移动时直接贴图零重绘。
  const staticKey = JSON.stringify([
    themeName, i18n.getLang(), pixelWidth, pixelHeight, ratio, state.chartTitle, state.chartTitleSettings, state.chartAxisSettings,
    ranges, model.labels,
    visibleSeries.map((series) => [series.key, series.axis, series.style, series.points.map((point) => point.value)])
  ]);
  if (!chartStaticCanvas) chartStaticCanvas = document.createElement("canvas");
  if (staticKey !== chartStaticKey || chartStaticCanvas.width !== pixelWidth || chartStaticCanvas.height !== pixelHeight) {
    chartStaticCanvas.width = pixelWidth;
    chartStaticCanvas.height = pixelHeight;
    const offscreen = chartStaticCanvas.getContext("2d");
    offscreen.setTransform(ratio, 0, 0, ratio, 0, 0);
    offscreen.fillStyle = theme.canvasBg; offscreen.fillRect(0, 0, width, height);
    offscreen.font = axisFont;
    offscreen.strokeStyle = theme.gridLine; offscreen.lineWidth = 1;
    const hasLeftAxis = visibleSeries.some((series) => series.axis === "left");
    const hasRightAxis = visibleSeries.some((series) => series.axis === "right");
    const showHorizontalGrid = (hasLeftAxis && state.chartAxisSettings.left.showGrid !== false) ||
      (hasRightAxis && state.chartAxisSettings.right.showGrid !== false);
    const formatAxisTick = (val, span) => {
      if (!Number.isFinite(val)) return "";
      const absSpan = Math.abs(span);
      if (absSpan >= 10000 || (absSpan > 0 && absSpan < 0.001)) return Number(val.toPrecision(5)).toString();
      const decimals = absSpan >= 100 ? 0 : absSpan >= 10 ? 1 : absSpan >= 1 ? 2 : 3;
      return val.toFixed(decimals);
    };
    const leftSpan = ranges.left.max - ranges.left.min;
    const rightSpan = ranges.right ? ranges.right.max - ranges.right.min : 0;
    for (let tick = 0; tick <= 5; tick++) {
      const value = ranges.left.min + leftSpan * tick / 5, y = pointY(value, "left");
      if (showHorizontalGrid) {
        offscreen.save();
        offscreen.setLineDash([3, 3]);
        offscreen.beginPath(); offscreen.moveTo(left, y); offscreen.lineTo(width - right, y); offscreen.stroke();
        offscreen.restore();
      }
      offscreen.fillStyle = theme.axisText; offscreen.textAlign = "right"; offscreen.fillText(formatAxisTick(value, leftSpan), left - 9, y + 4);
      if (ranges.right) { const rightValue = ranges.right.min + rightSpan * tick / 5; offscreen.textAlign = "left"; offscreen.fillText(formatAxisTick(rightValue, rightSpan), width - right + 9, y + 4); }
    }
    offscreen.strokeStyle = theme.axisLine; offscreen.beginPath(); offscreen.moveTo(left, top); offscreen.lineTo(left, height - bottom); offscreen.lineTo(width - right, height - bottom); offscreen.lineTo(width - right, top); offscreen.stroke();
    offscreen.fillStyle = theme.axisText; offscreen.textAlign = "center";
    const labelStep = Math.max(1, Math.round(Number(state.chartAxisSettings.x.labelStep) || 1), Math.ceil(model.labels.length / Math.max(1, plotWidth / 80)));
    const ticks = [...new Set([...Array.from({ length: model.labels.length }, (_unused, index) => index).filter((index) => index % labelStep === 0), Math.max(0, model.labels.length - 1)])];
    ticks.forEach((index) => offscreen.fillText(String(model.labels[index]).slice(0, 18), pointX(index), height - bottom + 24));
    offscreen.font = uiFont;
    offscreen.textAlign = "center"; if (state.chartAxisSettings.x.title) offscreen.fillText(state.chartAxisSettings.x.title, left + plotWidth / 2, height - bottom + 40);
    drawChartTitle(offscreen, width, height);
    offscreen.fillStyle = theme.axisText;
    const leftAxisLabel = (!state.chartAxisSettings.left.title || state.chartAxisSettings.left.title === "左轴") ? t("chart.leftAxis") : state.chartAxisSettings.left.title;
    const rightAxisLabel = (!state.chartAxisSettings.right.title || state.chartAxisSettings.right.title === "右轴") ? t("chart.rightAxis") : state.chartAxisSettings.right.title;
    offscreen.textAlign = "left"; offscreen.fillText(leftAxisLabel, 8, top - 12);
    offscreen.textAlign = "right"; offscreen.fillText(rightAxisLabel, width - 8, top - 12);
    chartStaticHits = [];
    visibleSeries.forEach((series, seriesIndex) => {
      const style = series.style || chartStyle(series.key, seriesIndex);
      offscreen.strokeStyle = style.lineColor || series.color; offscreen.fillStyle = style.pointColor || style.lineColor || series.color; offscreen.lineWidth = Math.max(1, Number(style.lineWidth) || 2); offscreen.setLineDash(chartLineDash(style)); offscreen.beginPath(); let connected = false;
      series.points.forEach((point, index) => {
        if (!Number.isFinite(point.value)) { connected = false; return; }
        const x = pointX(index), y = pointY(point.value, series.axis); if (connected) offscreen.lineTo(x, y); else offscreen.moveTo(x, y); connected = true;
        chartStaticHits.push({ x, y, point, seriesKey: series.key, seriesIndex, axis: series.axis, index });
      });
      if (style.lineStyle !== "none") offscreen.stroke();
      offscreen.setLineDash([]); series.points.forEach((point, index) => { if (!Number.isFinite(point.value)) return; drawChartPoint(offscreen, pointX(index), pointY(point.value, series.axis), style); });
    });
    chartStaticKey = staticKey;
  }
  state.chartHits = chartStaticHits;
  drawChartCursorOverlay();
}
function drawChartCursorOverlay() {
  if (chartOverlayFrame !== null) { cancelAnimationFrame(chartOverlayFrame); chartOverlayFrame = null; }
  const frame = state.chartFrame;
  if (!frame || !chartStaticCanvas) return;
  if (!el("tab-chart").classList.contains("active") || document.documentElement.classList.contains("chart-collapsed")) return;
  const canvas = el("chart");
  if (!canvas || canvas.hidden) return;
  const context = canvas.getContext("2d");
  const ratio = devicePixelRatio || 1;
  const { width, height, top, bottom, labels, model, xAt } = frame;
  const visibleSeries = model.series;
  const theme = chartThemeTokens();
  const axisFont = '11px "JetBrains Mono", "Consolas", "Microsoft YaHei", monospace';
  const uiFont = '11px "Inter", "PingFang SC", "Microsoft YaHei", sans-serif';
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.drawImage(chartStaticCanvas, 0, 0, width, height);
  const cursor = state.chartCursor;
  if (cursor.index >= 0 && cursor.index < labels.length) {
    const x = xAt(cursor.index);
    context.strokeStyle = theme.cursorLine; context.setLineDash([4, 4]); context.beginPath(); context.moveTo(x, top); context.lineTo(x, height - bottom); context.stroke(); context.setLineDash([]);
    const rows = visibleSeries.map((series) => ({ series, point: series.points[cursor.index] })).filter(({ point }) => point && Number.isFinite(point.value));
    const headerLine = `${state.chartDrag ? t("chart.dragging") : cursor.pinned ? t("chart.pinned") : t("chart.cursor")} · ${labels[cursor.index]}`;
    const entries = rows.map(({ series, point }) => ({
      color: series.style?.lineColor || series.color,
      label: `${series.name} [${series.axis === "right" ? t("chart.axisRightShort") : t("chart.axisLeftShort")}]${model.ordinal ? ` ${point.label}` : ""}`,
      value: display(point.record.row[point.field])
    }));
    context.font = `600 ${uiFont}`;
    const headerW = context.measureText(headerLine).width;
    let maxRowW = 0;
    for (const entry of entries) {
      context.font = uiFont; const lw = context.measureText(entry.label).width;
      context.font = axisFont; const vw = context.measureText(entry.value).width;
      maxRowW = Math.max(maxRowW, 14 + lw + 16 + vw);
    }
    const boxWidth = Math.min(340, Math.max(168, headerW + 22, maxRowW + 22));
    const boxHeight = (entries.length + 1) * 18 + 12;
    const boxX = x + 12 + boxWidth < width ? x + 12 : Math.max(6, x - boxWidth - 12);
    const boxY = Math.min(Math.max(6, cursor.y || top + 8), Math.max(6, height - bottom - boxHeight));
    context.fillStyle = theme.tooltipBg; context.strokeStyle = theme.tooltipBorder;
    context.beginPath();
    if (context.roundRect) context.roundRect(boxX, boxY, boxWidth, boxHeight, 6);
    else context.rect(boxX, boxY, boxWidth, boxHeight);
    context.fill(); context.stroke();
    context.font = `600 ${uiFont}`; context.fillStyle = theme.tooltipTitle; context.textAlign = "left";
    context.fillText(headerLine, boxX + 10, boxY + 18, boxWidth - 20);
    entries.forEach((entry, rowIndex) => {
      const y = boxY + 36 + rowIndex * 18;
      context.fillStyle = entry.color; context.beginPath(); context.arc(boxX + 14, y - 3, 3.5, 0, Math.PI * 2); context.fill();
      context.font = axisFont; context.fillStyle = theme.tooltipValue; context.textAlign = "right";
      context.fillText(entry.value, boxX + boxWidth - 10, y);
      const valW = context.measureText(entry.value).width;
      context.font = uiFont; context.fillStyle = theme.tooltipLabel; context.textAlign = "left";
      context.fillText(entry.label, boxX + 23, y, Math.max(20, boxWidth - 40 - valW));
    });
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
    rememberDragChange(hit); setPendingCell(hit.point.record, hit.point.column, formatChartValue(value, hit.point.column), false); syncGridCell(hit.point.record, hit.point.field);
  }
  updateEditActions();
  drag.previous = { x, y }; drag.moved = drag.changes.size > 0; state.chartCursor.index = nearest.index; state.chartCursor.y = y; scheduleChartDraw();
}
function finishChartDrag(cancel = false) {
  const drag = state.chartDrag; if (!drag) return; state.chartDrag = null;
  if (cancel) for (const change of drag.changes.values()) {
    change.record.row[change.column.eng_name] = change.before;
    const key = cellKey(change.record, change.column.eng_name); if (change.pending === undefined) state.pendingEdits.delete(key); else state.pendingEdits.set(key, { record: change.record, column: change.column, raw: change.pending });
    syncGridCell(change.record, change.column.eng_name);
  }
  if (!cancel && drag.moved) rememberEdit(drag.history);
  updateEditActions(); drawChart();
  if (drag.moved && !cancel) notice(`已拖拽修改 ${drag.changes.size} 个曲线点，保存后写入数据库`);
}
function chartPointerMove(event) {
  if (state.chartDrag) { updateChartDrag(event); return; }
  if (state.chartCursor.pinned) return;
  const frame = state.chartFrame; if (!frame) return; const { x, y } = chartCanvasPoint(event);
  if (x < frame.left || x > frame.left + frame.plotWidth || y < frame.top || y > frame.top + frame.plotHeight) { if (state.chartCursor.index !== -1) { state.chartCursor.index = -1; scheduleChartOverlay(); } return; }
  const index = frame.labels.length <= 1 ? 0 : Math.max(0, Math.min(frame.labels.length - 1, Math.round((x - frame.left) / frame.plotWidth * (frame.labels.length - 1))));
  if (state.chartCursor.index === index && state.chartCursor.y === y) return;
  state.chartCursor.index = index; state.chartCursor.y = y; scheduleChartOverlay();
}
function chartPointerDown(event) {
  const frame = state.chartFrame; if (state.busy || !frame || event.button !== 0) return;
  const { x, y } = chartCanvasPoint(event), hit = nearestChartHit(x, y);
  if (hit && state.editable && canDragChartPoint(hit.point) && Math.hypot(hit.x - x, hit.y - y) <= 12) {
    event.preventDefault(); state.chartDrag = { history: editSnapshot(), pointerId: event.pointerId, seriesKey: hit.seriesKey, ranges: structuredClone(frame.ranges), changes: new Map(), moved: false, previous: { x, y } }; el("chart").setPointerCapture(event.pointerId); return;
  }
  if (x >= frame.left && x <= frame.left + frame.plotWidth && y >= frame.top && y <= frame.top + frame.plotHeight) { state.chartCursor.pinned = !state.chartCursor.pinned; state.chartCursor.index = frame.labels.length <= 1 ? 0 : Math.max(0, Math.min(frame.labels.length - 1, Math.round((x - frame.left) / frame.plotWidth * (frame.labels.length - 1)))); state.chartCursor.y = y; drawChart(); }
}
function closeChartContextMenu(restoreFocus = false) {
  const menu = el("chart-context-menu");
  closeContextSubmenus(menu);
  menu.hidden = true;
  if (restoreFocus) el("chart").focus({ preventScroll: true });
}
function openChartContextMenu(event) {
  event.preventDefault(); event.stopPropagation();
  if (state.busy) return;
  closeAllContextMenus("chart");
  const menu = el("chart-context-menu"), hasSeries = buildChartModel().series.length > 0;
  menu.querySelector('[data-chart-action="clear-chart"]').disabled = !hasSeries;
  menu.querySelector('[data-chart-action="series-settings"]').disabled = !hasSeries;
  syncContextSubmenuGroups(menu);
  menu.hidden = false; menu.scrollTop = 0;
  const margin = 8, width = menu.offsetWidth, height = menu.offsetHeight;
  menu.style.left = `${Math.max(margin, Math.min(event.clientX, window.innerWidth - width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(event.clientY, window.innerHeight - height - margin))}px`;
  focusFirstContextItem(menu);
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
    const lineWidth = node("select"); lineWidth.dataset.setting = "lineWidth";
    [1, 1.5, 2, 2.5, 3, 4, 5, 6].forEach((value) => lineWidth.append(new Option(`${value} px`, String(value))));
    setSelectValueWithFallback(lineWidth, style.lineWidth, " px");
    const pointColor = node("input"); pointColor.type = "color"; pointColor.value = style.pointColor; pointColor.dataset.setting = "pointColor";
    const pointShape = node("select"); pointShape.dataset.setting = "pointShape"; [["圆点", "circle"], ["方点", "square"], ["菱形", "diamond"], ["三角形", "triangle"], ["隐藏", "none"]].forEach(([label, value]) => pointShape.append(new Option(label, value))); pointShape.value = style.pointShape;
    const pointSize = node("select"); pointSize.dataset.setting = "pointSize";
    [2, 3, 3.5, 4, 5, 6, 8, 10].forEach((value) => pointSize.append(new Option(`${value} px`, String(value))));
    setSelectValueWithFallback(pointSize, style.pointSize, " px");
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
    const axisHint = el("chart-axis-settings-hint");
    axisHint.textContent = "";
    axisHint.hidden = true;
    el("chart-axis-title").value = axisSettings.title || "";
    el("chart-axis-min").value = axis === "x" ? "" : axisSettings.min ?? "";
    el("chart-axis-max").value = axis === "x" ? "" : axisSettings.max ?? "";
    el("chart-axis-min-label").hidden = axis === "x"; el("chart-axis-max-label").hidden = axis === "x"; el("chart-x-step-label").hidden = axis !== "x"; el("chart-axis-grid-label").hidden = axis === "x";
    setSelectValueWithFallback(el("chart-x-step"), Math.max(1, Math.round(Number(axisSettings.labelStep) || 1)), "");
    el("chart-axis-grid").checked = axisSettings.showGrid !== false;
  } else if (kind === "title") {
    el("chart-title-input").value = state.chartTitle;
    const settings = state.chartTitleSettings;
    el("chart-title-color").value = settings.color; setSelectValueWithFallback(el("chart-title-size"), settings.fontSize, " px");
    el("chart-title-font").value = settings.fontFamily; el("chart-title-style").value = settings.fontStyle;
    el("chart-title-position").value = settings.position; setSelectValueWithFallback(el("chart-title-gap"), settings.gap, " px");
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
  article.append(node("p", "新表类型按文件全部数据行推断，默认首字段为 KEY；已有表保留当前定义，有数据时不能改变类型和 KEY。请逐表确认。", "import-warning"));
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
  if (efileImports.selected()) return efileImports.preview();
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
  if (definitionPreview?.upload_id) return efileImports.apply();
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

async function openDefinitionDialog() {
  if (hasPendingData() || state.schemaDirty) throw new Error("请先保存或撤销页面中的修改，再加载文件");
  el("definition-error").textContent = ""; resetDefinitionPreview(); await efileImports.restore(); el("definition-dialog").showModal();
}
async function backupSelectedTables() {
  if (!state.checkedTables.size) throw new Error("请在表索引中勾选要备份的表");
  const tables = [...state.checkedTables], result = await request("backup", "POST", { tables });
  download(result.text, result.filename, result.mime); log(`备份表：${tables.join(", ")}`);
}
function chooseRestoreBackup() {
  if (!state.busy) { el("restore-file").value = ""; el("restore-file").click(); }
}

function syncLayoutToggleButtons() {
  const sidebarOpen = !document.documentElement.classList.contains("sidebar-collapsed");
  const bottomOpen = !document.documentElement.classList.contains("chart-collapsed");
  const sidebarBtn = el("toggle-sidebar"), bottomBtn = el("toggle-bottom");
  if (sidebarBtn) { sidebarBtn.classList.toggle("is-active", sidebarOpen); sidebarBtn.setAttribute("aria-pressed", String(sidebarOpen)); }
  if (bottomBtn) { bottomBtn.classList.toggle("is-active", bottomOpen); bottomBtn.setAttribute("aria-pressed", String(bottomOpen)); }
}
const chartLayout = setupSplitter({ id: "chart-splitter", panelId: "bottom-panel", axis: "y", direction: -1, defaultSize: 248,
  minimum: chartMinimumHeight, maximum: () => {
    const workspace = document.querySelector(".workspace"), mainTabs = document.querySelector(".workspace > .tabs"), statusbar = document.querySelector(".statusbar");
    const mainPanel = el("tab-data").classList.contains("active") ? el("query-panel").getBoundingClientRect().height + el("query-splitter").getBoundingClientRect().height + 80 : 80;
    return Math.max(0, (workspace?.clientHeight || 0) - (mainTabs?.getBoundingClientRect().height || 0) - (statusbar?.getBoundingClientRect().height || 0) - mainPanel - el("chart-splitter").getBoundingClientRect().height);
  },
  collapseThreshold: 48, cssVariable: "--chart-height", collapsedClass: "chart-collapsed" });
setupSplitter({ id: "table-list-splitter", panelId: "table-list", axis: "y", defaultSize: 180,
  minimum: () => 72, maximum: () => Math.max(120, el("table-sidebar").clientHeight - 250),
  collapseThreshold: 44, cssVariable: "--table-list-height", collapsedClass: "table-list-collapsed" });
const sidebarLayout = setupSplitter({ id: "sidebar-splitter", panelId: "table-sidebar", axis: "x", defaultSize: window.innerWidth <= 720 ? 0 : 248,
  minimum: () => 176, maximum: () => Math.min(480, document.querySelector(".layout").clientWidth - 320),
  collapseThreshold: 56, cssVariable: "--sidebar-width", collapsedClass: "sidebar-collapsed" });
setupSplitter({ id: "query-splitter", panelId: "query-panel", axis: "y", defaultSize: 48,
  minimum: () => 44, maximum: () => 52,
  collapseThreshold: 16, cssVariable: "--query-height", collapsedClass: "query-collapsed" });
syncLayoutToggleButtons();
if (el("toggle-sidebar")) el("toggle-sidebar").onclick = () => { sidebarLayout.toggle(); syncLayoutToggleButtons(); };
if (el("toggle-bottom")) el("toggle-bottom").onclick = () => { chartLayout.toggle(); syncLayoutToggleButtons(); requestAnimationFrame(drawChart); };
if (el("lang-select")) el("lang-select").onchange = () => i18n.setLang(el("lang-select").value);
if (el("theme-select")) el("theme-select").onchange = () => applyTheme(el("theme-select").value);
i18n.onChange(() => {
  chartStaticKey = null;
  legendSignature = null;
  if (state.table && typeof renderGrid === "function") renderGrid();
  if (typeof drawChart === "function" && el("tab-chart")) requestAnimationFrame(drawChart);
});

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
bindContextMenuNavigation(el("chart-context-menu"), closeChartContextMenu);
el("chart-area").addEventListener("contextmenu", openChartContextMenu);
el("records-panel").addEventListener("contextmenu", (event) => openRowContextMenu(event, null, null));
el("table-list").addEventListener("contextmenu", (event) => openSidebarContextMenu(event, "tables"));
el("field-list").addEventListener("contextmenu", (event) => openSidebarContextMenu(event, "fields"));
el("data-grid").addEventListener("keydown", (event) => {
  if (event.target.closest(".cell-editor") || !(event.ctrlKey || event.metaKey) || event.altKey) return;
  if (event.key.toLowerCase() !== "c") return;
  event.preventDefault(); event.stopPropagation(); void run(copySelectedCells);
});
function clearSelectedCells() {
  if (!state.editable || isReadOnlyDatabase()) return;
  const model = gridSelectionModel(); if (!model) return;
  const useCellSet = state.selectedCells.size > 0;
  const targets = [];
  for (const rowIndex of model.rows) {
    const record = state.records[rowIndex]; if (!record) continue;
    for (const column of model.columns) {
      if (useCellSet && !state.selectedCells.has(cellSelectionKey(rowIndex, column.eng_name))) continue;
      if (isReadOnlyKey(record, column)) continue;
      targets.push({ record, column });
    }
  }
  if (!targets.length) return;
  const history = editSnapshot();
  targets.forEach(({ record, column }) => setPendingCell(record, column, "", false));
  rememberEdit(history);
  targets.forEach(({ record, column }) => syncGridCell(record, column.eng_name));
  paintGridSelection(); drawChart(); updateEditActions();
  notice(`已清空 ${targets.length} 个单元格，Ctrl+S 保存或 Ctrl+Z 撤销`);
}
el("data-grid").addEventListener("keydown", (event) => {
  if (state.busy || event.isComposing || event.altKey || event.target.matches("input, select, textarea")) return;
  const control = event.ctrlKey || event.metaKey;
  if (control && event.key.toLowerCase() === "a") {
    event.preventDefault(); selectGridCells(0, 0, state.records.length - 1, visibleGridColumns().length - 1); return;
  }
  if (control && !["Home", "End"].includes(event.key)) return;
  if (event.key === "Escape") { clearGridSelection(); paintGridSelection(); return; }
  const cell = event.target.closest("[data-cell-row][data-cell-column]");
  const row = cell ? Number(cell.dataset.cellRow) : state.cellAnchor?.row || 0;
  const column = cell ? Number(cell.dataset.cellColumn) : state.cellAnchor?.column || 0;
  if (event.key === "F2" && cell && state.editable && !isReadOnlyDatabase()) {
    const record = state.records[row], col = visibleGridColumns()[column];
    if (record && col && !isReadOnlyKey(record, col)) {
      event.preventDefault();
      if (!inlineCellInput(record, col, row, cell)) run(() => openRow("edit", record));
    }
    return;
  }
  if ((event.key === "Delete" || event.key === "Backspace") && !control && state.editable && !isReadOnlyDatabase()) {
    event.preventDefault(); clearSelectedCells(); return;
  }
  if (!control && event.key.length === 1 && cell && state.editable && !isReadOnlyDatabase()) {
    const record = state.records[row], col = visibleGridColumns()[column];
    if (record && col && !isReadOnlyKey(record, col) && !boolTypes.has(columnKind(col)) && !objectTypes.has(columnKind(col)) && !columnMenuOptions(col).length) {
      event.preventDefault();
      inlineCellInput(record, col, row, cell, event.key);
      return;
    }
  }
  if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "Enter", "Tab"].includes(event.key)) return;
  if (event.key === "Tab" && ((event.shiftKey && row === 0 && column === 0) ||
      (!event.shiftKey && row === state.records.length - 1 && column === visibleGridColumns().length - 1))) return;
  event.preventDefault();
  moveGridFocus(row, column, event.key, event.shiftKey, control, event.shiftKey && !["Tab", "Enter"].includes(event.key));
});
el("dismiss-notice").onclick = () => { clearTimeout(noticeTimer); el("operation-notice").hidden = true; };
el("save-preview-edits").onclick = () => { el("edit-preview-dialog").close(); run(saveCellEdits); };
document.addEventListener("keydown", (event) => {
  if (state.busy || !(event.ctrlKey || event.metaKey) || event.altKey || event.isComposing ||
      event.target.closest("input,textarea,select") || document.querySelector("dialog[open]") || !el("tab-data").classList.contains("active")) return;
  const key = event.key.toLowerCase();
  if (key === "z" || key === "y") { event.preventDefault(); restoreEdit(key === "y" || event.shiftKey); }
});
document.addEventListener("keydown", (event) => {
  if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "s" || event.altKey || event.isComposing || document.querySelector("dialog[open]")) return;
  event.preventDefault();
  if (state.busy) return;
  if (el("tab-schema").classList.contains("active")) { if (state.schemaDirty) run(saveSchema); else notice("表结构没有待保存修改"); }
  else if (hasPendingData()) { document.activeElement?.blur(); run(saveCellEdits); }
  else notice("没有待保存修改");
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
for (const id of ["table-list", "field-list"]) {
  el(id).addEventListener("keydown", (event) => {
    if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
    event.preventDefault();
    const bounds = event.currentTarget.getBoundingClientRect();
    event.currentTarget.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: bounds.left + 8, clientY: bounds.top + 8 }));
  });
}
bindContextMenuNavigation(el("row-context-menu"), closeRowContextMenu);
bindContextMenuNavigation(el("sidebar-context-menu"), closeSidebarContextMenu);
bindContextMenuNavigation(el("schema-context-menu"), closeSchemaContextMenu);
document.addEventListener("click", (event) => { if (!event.target.closest("#row-context-menu")) closeRowContextMenu(); if (!event.target.closest("#chart-context-menu")) closeChartContextMenu(); if (!event.target.closest("#schema-context-menu")) closeSchemaContextMenu(); if (!event.target.closest("#sidebar-context-menu")) closeSidebarContextMenu(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !el("row-context-menu").hidden) { event.preventDefault(); closeRowContextMenu(true); } });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !el("chart-context-menu").hidden) { event.preventDefault(); closeChartContextMenu(true); } });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !el("schema-context-menu").hidden) { event.preventDefault(); closeSchemaContextMenu(true); } });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !el("sidebar-context-menu").hidden) { event.preventDefault(); closeSidebarContextMenu(true); } });
document.addEventListener("contextmenu", (event) => { if (!event.target.closest("#records-panel, #row-context-menu")) closeRowContextMenu(); if (!event.target.closest("#chart-area, #chart-context-menu")) closeChartContextMenu(); if (!event.target.closest("#schema-fields, #schema-context-menu")) closeSchemaContextMenu(); if (!event.target.closest("#table-list, #field-list, #sidebar-context-menu")) closeSidebarContextMenu(); });
document.addEventListener("scroll", (event) => { if (!el("row-context-menu").contains(event.target)) closeRowContextMenu(); if (!el("chart-context-menu").contains(event.target)) closeChartContextMenu(); if (!el("schema-context-menu").contains(event.target)) closeSchemaContextMenu(); if (!el("sidebar-context-menu").contains(event.target)) closeSidebarContextMenu(); }, true);
document.addEventListener("pointermove", updateCellSelectionFromPointer);
document.addEventListener("pointerup", finishCellSelection);
document.addEventListener("pointercancel", finishCellSelection);
window.addEventListener("blur", finishCellSelection);
window.addEventListener("blur", () => { closeRowContextMenu(); closeChartContextMenu(); closeSchemaContextMenu(); closeSidebarContextMenu(); });
window.addEventListener("resize", () => { closeRowContextMenu(); closeChartContextMenu(); closeSchemaContextMenu(); closeSidebarContextMenu(); });
let tableFilterTimer = null;
el("table-filter").addEventListener("input", () => {
  clearTimeout(tableFilterTimer);
  tableFilterTimer = setTimeout(renderTables, 120);
});
el("table-filter").addEventListener("keydown", (event) => {
  if (event.key === "Escape" && el("table-filter").value) {
    event.preventDefault();
    el("table-filter").value = "";
    renderTables();
  } else if (event.key === "Enter") {
    const first = filteredTables().find((item) => !item.orphan && item.selectable !== false);
    if (first && first.name !== state.table) {
      event.preventDefault();
      run(async () => { if (await discardSchema() && await discardCellEdits()) await selectTable(first.name); });
    }
  }
});
for (const id of ["exact-match", "nonempty"]) el(id).addEventListener("input", renderTables);
if (el("current-database-file")) el("current-database-file").onclick = () => run(() => openDatabaseFileDialog("server"));
if (el("open-tools-menu")) {
  el("open-tools-menu").onclick = (event) => {
    event.stopPropagation();
    const rect = el("open-tools-menu").getBoundingClientRect();
    openSidebarContextMenu({
      preventDefault() {},
      stopPropagation() {},
      target: el("open-tools-menu"),
      clientX: rect.left,
      clientY: rect.bottom + 6
    }, "tables");
  };
}
if (el("open-shortcuts")) el("open-shortcuts").onclick = () => el("shortcuts-dialog")?.showModal();
document.addEventListener("keydown", (event) => {
  if (event.key === "?" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.target.closest("input,textarea,select") && !document.querySelector("dialog[open]")) {
    event.preventDefault();
    el("shortcuts-dialog")?.showModal();
  }
});
if (el("preview-cell-edits")) el("preview-cell-edits").onclick = () => openEditPreview();
if (el("schema-indexes-btn")) el("schema-indexes-btn").onclick = () => run(() => schemaTools.openIndexes());
if (el("schema-preview-btn")) el("schema-preview-btn").onclick = () => run(() => schemaTools.preview());
if (el("schema-field-filter")) el("schema-field-filter").oninput = filterSchemaFieldRows;
el("table-list").addEventListener("keydown", (event) => {
  if (!["ArrowDown", "ArrowUp"].includes(event.key) || state.busy) return;
  const selectable = filteredTables().filter((item) => !item.orphan && item.selectable !== false);
  if (!selectable.length) return;
  event.preventDefault();
  const current = selectable.findIndex((item) => item.name === state.table);
  const delta = event.key === "ArrowDown" ? 1 : -1;
  const nextIndex = current < 0 ? (delta > 0 ? 0 : selectable.length - 1) : Math.max(0, Math.min(selectable.length - 1, current + delta));
  const target = selectable[nextIndex];
  if (target && target.name !== state.table) {
    run(async () => {
      if (await discardSchema() && await discardCellEdits()) {
        await selectTable(target.name);
        el("table-list").querySelector(".table-entry.active")?.scrollIntoView({ block: "nearest" });
      }
    });
  }
});
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && !event.altKey && !event.isComposing && !document.querySelector("dialog[open]")) {
    event.preventDefault();
    el("table-filter").focus({ preventScroll: true });
    el("table-filter").select();
  }
});
el("refresh").onclick = () => run(refresh);
el("query").onclick = () => run(executeSql);
el("filter-field").onchange = syncQuickFilterValueControl;
el("filter-op").onchange = syncQuickFilterValueControl;
el("apply-quick-filter").onclick = () => run(applyQuickFilter);
el("reset-quick-filter").onclick = () => run(resetQuickFilter);
el("btn-add-row").onclick = () => run(() => openAddRows(null, null));
el("btn-batch-edit").onclick = () => run(() => openRow("batch"));
el("btn-import-data").onclick = () => run(openImportDialog);
el("export-format").onchange = updateRecordsToolbar;
el("btn-export-data").onclick = () => run(() => exportQuery(el("export-format").value));
el("btn-delete-rows").onclick = () => run(() => deleteSelectedRows(null, null));
el("page-size").onchange = () => {
  if (state.busy) { el("page-size").value = String(state.pageSize || 100); return; }
  run(async () => {
    try { await queryRows({ reset: true }); }
    catch (error) { el("page-size").value = String(state.pageSize || 100); throw error; }
  });
};
el("chinese-columns").onchange = () => { renderGrid(); drawChart(); };
if (el("value-display-mode")) {
  try { applyValueDisplayMode(localStorage.getItem(VALUE_DISPLAY_MODE_KEY) || "display", false); } catch { applyValueDisplayMode("display", false); }
  el("value-display-mode").onchange = () => { applyValueDisplayMode(el("value-display-mode").value); renderGrid(); };
}
el("prev").onclick = () => run(() => queryRows({ offset: Math.max(0, state.offset - Number(el("page-size").value)), pageCursor:state.previousCursor }));
el("next").onclick = () => run(() => queryRows({ offset: state.offset + Number(el("page-size").value), pageCursor:state.nextCursor }));
el("page-number").onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); el("go-page").click(); } };
el("go-page").onclick = () => run(async () => { const page = Number(el("page-number").value), size = Number(el("page-size").value);
  try {
    if (!Number.isInteger(page) || page < 1 || page > Math.max(1, Math.ceil(state.total / size))) throw new Error("页码超出范围");
    await queryRows({ offset: (page - 1) * size });
  } finally { updateGridPager(); }
});
el("save-cell-edits").onclick = () => run(saveCellEdits);
el("discard-cell-edits").onclick = () => run(discardCellEdits);
bindPresetControl(el("add-row-preset"), el("add-row-count"));
el("add-rows-form").onsubmit = (event) => {
  event.preventDefault(); run(addBlankRows, "add-rows-error");
};
document.querySelectorAll("[data-add-rows-cancel]").forEach((button) => { button.onclick = () => el("add-rows-dialog").close(); });
el("batch-add-field").onclick = () => {
  const field = el("batch-field-select").value;
  if (!field) { el("row-error").textContent = "请先从下拉框选择要修改的字段"; return; }
  el("row-error").textContent = "";
  addBatchFieldRow(field);
};
el("batch-field-select").onkeydown = (event) => { if (event.key === "Enter" && el("batch-field-select").value) { event.preventDefault(); el("batch-add-field").click(); } };
el("save-row").onclick = () => run(saveRow, "row-error");
el("json-mode").onchange = () => {
  const isBatch = state.dialog?.mode === "batch";
  try {
    if (el("json-mode").checked) {
      let currentValues = {};
      if (isBatch) {
        const list = el("batch-fields-list");
        currentValues = list.querySelectorAll(".batch-field-row").length ? readBatchFields() : {};
      } else {
        currentValues = readRowFields();
      }
      el("row-editor").value = JSON.stringify(currentValues, null, 2);
      el("row-fields").hidden = true; el("batch-fields-panel").hidden = true; el("row-editor").hidden = false;
    } else {
      const parsed = JSON.parse(el("row-editor").value || "{}");
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("记录须为 JSON 对象");
      if (isBatch) {
        buildBatchFields(parsed);
        el("row-fields").hidden = true; el("batch-fields-panel").hidden = false; el("row-editor").hidden = true;
      } else {
        buildRowFields(parsed);
        el("row-fields").hidden = false; el("batch-fields-panel").hidden = true; el("row-editor").hidden = true;
      }
    }
    el("row-error").textContent = "";
  } catch (error) { el("json-mode").checked = !el("json-mode").checked; el("row-error").textContent = error.message; }
};
el("new-table").onclick = () => run(async () => {
  if (!state.database) throw new Error("请先选择或新建数据库");
  if (!await discardSchema() || !await discardCellEdits()) return; resetSelection();
  renderSchema({ head: { eng_name: "", chn_name: "" }, columns: [{ eng_name: "id", chn_name: "编号", data_type: "int", primary_key: true, disp_type: "整数", disp_menu: "" },
    { eng_name: "name", chn_name: "名称", data_type: "str", primary_key: false, disp_type: "文本", disp_menu: "" }] }); tab("schema");await localDrafts.offer();
});
async function openDatabaseFileDialog(fileTab = "server") {
  if (!await discardSchema() || !await discardCellEdits()) return;
  await loadWorkspaceDirectories({ reset: true });
  el("database-file-create").open = false;
  setDatabaseFileTab(fileTab);
  el("database-file-dialog").showModal();
  if (fileTab === "server") showWorkspacePicker();
}
el("app-title").ondblclick = () => run(openDatabaseFileDialog);
el("open-database-files").onclick = () => run(openDatabaseFileDialog);
bindWorkspacePicker();
el("database-select").onchange = () => {
  const filename = el("database-select").value; el("database-select").value = state.database || "";
  if (state.busy) return;
  run(async () => {
    if (!await discardSchema() || !await discardCellEdits()) return;
    await activateDatabase(filename);
  });
};
document.querySelectorAll("[data-database-file-tab]").forEach((button) => {
  button.onclick = () => run(async () => {
    const fileTab = button.dataset.databaseFileTab;
    const directory = workspaceTree.browsingPath;
    if (fileTab === "upload" && directory !== workspaceTree.currentPath) {
      canSwitchWorkspace();
      if (!await discardSchema() || !await discardCellEdits()) return;
      navigateWorkspace(directory, { choose: "upload" });
      return;
    }
    setDatabaseFileTab(fileTab);
    if (fileTab === "server") showWorkspacePicker();
  });
});
el("refresh-database-files").onclick = () => run(async () => {
  await loadWorkspaceDirectories();
  await refreshWorkspaceFiles(workspaceTree.browsingPath);
});
el("database-repair-embed").onclick = () => run(() => repairDatabase("rebuild-embedded-definition"), "database-repair-error");
el("database-repair-delete").onclick = () => run(() => repairDatabase("delete-orphan"), "database-repair-error");
el("database-repair-rebuild").onclick = () => run(() => repairDatabase("rebuild-from-embedded"), "database-repair-error");
el("database-repair-dialog").addEventListener("close", () => {
  state.databaseRepairFile = null;
  if (el("database-file-dialog").open && !el("database-file-server").hidden) showWorkspacePicker();
});
el("database-file-upload-input").onchange = renderDatabaseUploadSelection;
el("upload-database-file").onclick = () => run(uploadDatabaseFile, "database-file-error");
el("create-database-file").onclick = () => run(createDatabaseFile);
el("schema-name").oninput = schemaChanged; el("schema-label").oninput = schemaChanged;
el("add-column").onclick = () => run(() => {
  const tbody = el("schema-fields").querySelector("tbody");
  if (!tbody) {
    renderSchema({ head: { eng_name: el("schema-name").value.trim(), chn_name: el("schema-label").value.trim() }, columns: [{ eng_name: "field_1", chn_name: "新字段", data_type: "str", primary_key: false, disp_type: "文本", disp_menu: "" }] });
    schemaChanged();
    return;
  }
  const existingNames = new Set([...tbody.querySelectorAll('[data-property="eng_name"]')].map((input) => input.value.trim()));
  let number = tbody.children.length + 1;
  while (existingNames.has(`field_${number}`)) number++;
  const newCol = { eng_name: `field_${number}`, chn_name: "新字段", data_type: "str", primary_key: false, disp_type: "文本", disp_menu: "" };
  const row = createSchemaFieldRow(newCol);
  tbody.append(row);
  row.dataset.editing = "true";
  updateSchemaLocks();
  schemaChanged();
  row.scrollIntoView({ block: "nearest" });
  const nameInput = row.querySelector('[data-property="eng_name"]');
  if (nameInput) { nameInput.disabled = false; nameInput.focus({ preventScroll: true }); nameInput.select(); }
});
el("save-schema").onclick = () => run(saveSchema);
el("export-schema").onclick = () => run(() => { const definition = schemaFormValue(); download(JSON.stringify(definition, null, 2), `${definition.head.eng_name || "table"}.schema.json`, "application/json"); });
el("check-table").onclick = () => run(async () => {
  requireTable();
  const result = await request(tablePath("check"));
  const storage = result.storage || {};
  const modeText = `${String(storage.journal_mode || "WAL").toUpperCase()} / ${storage.synchronous ?? "FULL"}`;
  const message = `${state.table}：${result.ok ? "完整性检查通过" : "检查失败"}，${result.actual_count} 条，${modeText}`;
  notice(message, !result.ok); log(message, !result.ok);
});
el("delete-schema").onclick = () => run(async () => {
  requireTable(); const name = state.table;
  if (!await confirmAction("删除空表", `仅允许删除空表 ${name}，数据库与定义将归档保存。`, name)) return;
  await request(tablePath("schema"), "DELETE", { confirm: name, version: state.version }); state.schemaDirty = false; state.table = null; await refresh(); notice(`已归档删除空表：${name}`); log(`删除空表 ${name}`);
});
el("sql-history-toggle").onpointerdown = (event) => event.preventDefault();
el("sql-history-toggle").onclick = () => { if (!el("sql-history-list").hidden) closeSqlHistory(); else { el("sql-editor").focus(); openSqlHistory(); } };
el("sql-editor").onblur = closeSqlHistory;
el("sql-editor").oninput = () => { if (!el("sql-history-list").hidden && state.history.length) openSqlHistory(el("sql-editor").value); syncQuickFilterValueControl(); };
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
["definition-file", "definition-encoding", "definition-mode", "definition-keys"].forEach((id) => el(id).addEventListener("input", () => { if (efileImports.selected()) efileImports.optionsChanged(id); else { resetDefinitionPreview(); efileImports.controls(); } }));
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
el("save-chart").onclick = () => run(() => { drawChart(); if (el("chart").hidden) throw new Error("没有可导出的曲线"); const anchor = node("a"); anchor.href = el("chart").toDataURL("image/png"); anchor.download = `${state.table}-chart.png`; anchor.click(); });
const queryAuditLogsFromControls = () => run(async () => {
  state.logQuery = { ...state.logQuery, date: el("log-date").value, keyword: el("log-keyword").value.trim(),
    category: el("log-category").value, status: el("log-status").value,
    pageSize: Number(el("log-page-size").value), page: 1 };
  await loadAuditLogs();
});
el("query-logs").onclick = queryAuditLogsFromControls;
for (const id of ["log-date", "log-category", "log-status", "log-page-size"]) el(id).onchange = queryAuditLogsFromControls;
bindPresetControl(el("log-retention-preset"), el("log-retention-days"));
el("task-history").onclick = () => run(()=>backgroundTasks.history());
el("log-keyword").onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); el("query-logs").click(); } };
el("refresh-logs").onclick = () => run(async () => { await loadAuditLogs(); });
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
  const days = Number(state.logConfig?.retention_days ?? el("log-retention-days")?.value ?? 0);
  const desc = days > 0 ? `将按当前保留策略（${days} 天）永久清理过期操作日志，此操作不可撤销。` : "当前保留策略为永久（0 天），将执行安全检查与空日志归整，确认继续？";
  if (!await confirmAction("立即清理日志", desc)) return;
  const result = await request("logs/cleanup", "POST", {});
  state.logDates = result.dates || state.logDates; state.logQuery.page = 1;
  renderLogDates(); await loadAuditLogs();
  notice(`日志清理完成：删除 ${result.deleted_files?.length || 0} 个文件、${result.deleted_records || 0} 条记录`);
});
el("auto-refresh").onchange = () => {
  clearInterval(state.timer); const seconds = Number(el("auto-refresh").value);
  updateAutoRefreshStatus();
  if (seconds) state.timer = setInterval(() => {
    updateAutoRefreshStatus();
    if (!autoRefreshPauseReason()) run(() => queryRows({ silent: true }));
  }, seconds * 1000);
};
document.addEventListener("visibilitychange", updateAutoRefreshStatus);
document.addEventListener("keydown", (event) => { if (event.key === "F5") { event.preventDefault(); if (!document.querySelector("dialog[open]")) run(refresh); } });
window.addEventListener("beforeunload", (event) => { void localDrafts.flush(true);if (state.schemaDirty || hasPendingData()) { event.preventDefault(); event.returnValue = ""; } });
window.addEventListener('pagehide',()=>{void localDrafts.flush(true);localDrafts.announce('bye');});
let chartResizeFrame;
el("last-refresh").ondblclick = () => uiPerformance.show();
el("export-performance").onclick = () => uiPerformance.export();
const chartObserver = new ResizeObserver(() => {
  cancelAnimationFrame(chartResizeFrame);
  chartResizeFrame = requestAnimationFrame(drawChart);
});
chartObserver.observe(el("chart-area"));
virtualGrid.init();
schemaTools.init();
run(async () => {
  let info;
  try {
    info = await request("info");
  } catch (error) {
    el("connection").classList.remove("is-connecting", "is-online");
    el("connection").classList.add("is-error");
    el("connection").textContent = "连接异常";
    if (!workspacePath || ![400, 403, 404].includes(error.status)) throw error;
    const fallback = new URL(location.href);
    fallback.searchParams.delete("workspace");
    fallback.searchParams.delete("database");
    fallback.searchParams.set("choose_database", "1");
    location.replace(fallback.href);
    return;
  }
  el("connection").classList.remove("is-connecting", "is-error");
  el("connection").classList.add("is-online");
  el("connection").textContent = `SQLite ${info.sqlite_version} · WAL / FULL`;
  configureWorkspaceScope(info);
  if (await restoreWorkspaceSelection(info)) return;
  state.historyScope = workspaceScope || `${info.data_dir}\n${info.schema_dir}`;
  await localDrafts.init();
  state.dataDir = info.data_dir || "";
  el("storage-path").textContent = info.data_dir; el("storage-path").title = `数据：${info.data_dir}\n定义：${info.schema_dir}`;
  log("管理终端已连接 SQLite");
  await backgroundTasks.resume();
  const parameters = new URLSearchParams(location.search);
  const requestedDatabase = parameters.get("database");
  const chooseDatabase = parameters.get("choose_database");
  const previousDatabase = rememberedDatabase();
  const filename = requestedDatabase || (!parameters.has("database") && !chooseDatabase ? previousDatabase : null);
  if (filename) {
    try {
      await selectDatabaseFile(filename);
      return;
    } catch (error) {
      if (![400, 403, 404, 409, 422].includes(error.status)) throw error;
      if (filename === previousDatabase) rememberWorkspaceSelection(null);
      await refreshTables(); if (state.tables.length) await selectTable(state.tables[0].name); else resetSelection();
      await openDatabaseFileDialog();
      el("workspace-path-error").textContent = `无法打开 ${filename}，请选择其他数据库。`;
      return;
    }
  }
  await refreshTables(); if (state.tables.length) await selectTable(state.tables[0].name); else resetSelection();
  if (chooseDatabase === "1" || chooseDatabase === "upload") {
    rememberWorkspaceSelection();
    await openDatabaseFileDialog(chooseDatabase === "upload" ? "upload" : "server");
  } else {
    rememberWorkspaceSelection(parameters.has("database") ? null : undefined);
  }
});
