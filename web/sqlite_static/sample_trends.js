/* RTDB 管理终端 · 采样点定义与实时双轴采样点曲线板
 * 参考 SCADA PointTrendHistory / point-trend.js / point-trends-page.js 设计，
 * 完整适配 RTDB 多数据库、8 套 data-theme 视觉主题与中/英/俄三语体系。
 */
(function () {
  const MAX_POINTS = 24;
  const DEFAULT_PALETTE = [
    "#2563EB", "#0D9488", "#EA580C", "#7C3AED",
    "#DC2626", "#0284C7", "#16A34A", "#CA8A04",
    "#DB2777", "#4F46E5", "#0891B2", "#65A30D"
  ];
  const INTERVAL_PRESETS = [1, 2, 5, 10, 15, 30, 60];
  const RETENTION_PRESETS_MIN = [15, 30, 60, 180, 360, 720, 1440];
  const WINDOW_PRESETS_MIN = [5, 15, 30, 60, 180, 360, 720, 1440];
  const LAYOUT_STORAGE_KEY = "rtdb.sampleTrends.layout";
  const NUMERIC_OR_BOOL_TYPES = new Set([
    "int", "int8", "int16", "int32", "int64", "integer", "tinyint", "smallint", "mediumint", "bigint",
    "uint", "uint8", "uint16", "uint32", "uint64", "unsigned",
    "float", "float32", "float64", "double", "real", "decimal", "numeric", "number",
    "bool", "boolean", "bit"
  ]);
  const BOOL_TYPES = new Set(["bool", "boolean", "bit"]);

  const el = (id) => document.getElementById(id);
  const tr = (key, params, fallback) => {
    const i18n = window.rtdbI18n;
    if (i18n && typeof i18n.t === "function") {
      const value = i18n.t(key, params);
      if (value && value !== key) return value;
    }
    if (!fallback) return key;
    if (!params) return fallback;
    return fallback.replace(/\{(\w+)\}/g, (_, k) => (params[k] == null ? "" : String(params[k])));
  };

  function pointKey(table, key, field) {
    return JSON.stringify([String(table), Array.isArray(key) ? key : [key], String(field)]);
  }

  function isSampleableType(dataType) {
    if (!dataType) return false;
    const normalized = String(dataType).trim().toLowerCase().replace(/\(.*\)$/, "");
    return NUMERIC_OR_BOOL_TYPES.has(normalized);
  }

  function isBoolType(dataType) {
    if (!dataType) return false;
    const normalized = String(dataType).trim().toLowerCase().replace(/\(.*\)$/, "");
    return BOOL_TYPES.has(normalized);
  }

  function defaultSettings() {
    return {
      interval_seconds: 5,
      retention_seconds: 3600,
      series: [],
      view: {
        open: true,
        range_mode: "rolling",
        window_minutes: 15,
        step_seconds: 0,
        from: "",
        until: "",
        axes: {
          left: { auto: true, min: null, max: null, unit: "" },
          right: { auto: true, min: null, max: null, unit: "" }
        }
      }
    };
  }

  function clone(obj) {
    return JSON.parse(JSON.stringify(obj));
  }

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function formatDateTimeInput(ms) {
    if (!Number.isFinite(ms)) return "";
    const d = new Date(ms);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  function parseDateTimeInput(text) {
    if (!text || typeof text !== "string") return null;
    const ms = Date.parse(text);
    return Number.isFinite(ms) ? ms : null;
  }

  function formatTimeShort(ms, showDate = false) {
    if (!Number.isFinite(ms)) return "—";
    const d = new Date(ms);
    const hms = `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
    if (!showDate) return hms;
    return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hms}`;
  }

  function formatFullTime(ms) {
    if (!Number.isFinite(ms)) return "—";
    const d = new Date(ms);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  function formatNumber(value, kind) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    const n = Number(value);
    if (kind === "switch") return n !== 0 ? "1" : "0";
    if (Number.isInteger(n)) return String(n);
    const abs = Math.abs(n);
    if (abs >= 1000) return n.toFixed(2).replace(/\.?0+$/, "");
    if (abs >= 1) return n.toFixed(4).replace(/\.?0+$/, "");
    return n.toPrecision(4).replace(/\.?0+$/, "");
  }

  function niceTicks(minVal, maxVal, count) {
    if (!Number.isFinite(minVal) || !Number.isFinite(maxVal)) return [0, 1];
    if (minVal === maxVal) {
      const pad = minVal === 0 ? 1 : Math.abs(minVal) * 0.1;
      minVal -= pad;
      maxVal += pad;
    }
    const span = maxVal - minVal;
    const rough = span / Math.max(2, count);
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    const norm = rough / mag;
    let step = mag;
    if (norm <= 1.2) step = 1 * mag;
    else if (norm <= 2.5) step = 2 * mag;
    else if (norm <= 5.5) step = 5 * mag;
    else step = 10 * mag;
    const start = Math.floor(minVal / step) * step;
    const end = Math.ceil(maxVal / step) * step;
    const ticks = [];
    for (let v = start; v <= end + step * 0.25; v += step) {
      ticks.push(Math.abs(v) < step * 1e-9 ? 0 : Number(v.toPrecision(10)));
      if (ticks.length > 20) break;
    }
    return ticks.length >= 2 ? ticks : [minVal, maxVal];
  }

  function themeTokens() {
    const style = getComputedStyle(document.documentElement);
    const get = (name, fallback) => (style.getPropertyValue(name) || "").trim() || fallback;
    const scheme = (style.colorScheme || "").toLowerCase();
    const dark = scheme.includes("dark");
    return {
      dark,
      bg: get("--bg-canvas", dark ? "#0B0F19" : "#F8FAFC"),
      surface: get("--bg-surface", dark ? "#111827" : "#FFFFFF"),
      text: get("--text-primary", dark ? "#F8FAFC" : "#0F172A"),
      textSecondary: get("--text-secondary", dark ? "#CBD5E1" : "#475569"),
      muted: get("--text-muted", dark ? "#94A3B8" : "#64748B"),
      border: get("--border", dark ? "rgba(255,255,255,0.10)" : "#E2E8F0"),
      grid: dark ? "rgba(148, 163, 184, 0.16)" : "rgba(100, 116, 139, 0.16)",
      accent: get("--accent", "#2563EB"),
      danger: get("--danger", "#DC2626"),
      warning: get("--warning", "#D97706"),
      fontSans: get("--font-sans", "Segoe UI, sans-serif"),
      fontMono: get("--font-mono", "Consolas, monospace")
    };
  }

  const state = {
    host: null,
    workspace: "",
    database: "",
    active: false,
    initialized: false,
    loading: false,
    saving: false,
    historyLoaded: false,
    revision: null,
    serverOffsetMs: 0,
    lastSyncMs: 0,
    lastSampleMs: 0,
    settings: defaultSettings(),
    samples: [],        // [{ time_ms, samples: { [key]: { v, q, ok, f } } }]
    seriesMap: new Map(), // key -> series item
    focusedKey: null,
    error: "",
    pollTimer: null,
    wheelTimer: null,
    hover: null,        // { x, y, timeMs, canvasX }
    legendHits: [],     // [{ key, x, y, w, h, overflow }]
    contextSeriesKey: null,
    addDialog: {
      tables: [],
      schema: null,
      rows: [],
      loadingRows: false
    },
    layout: {
      mode: "split",    // "split" | "chart-full" | "list-full"
      listRatio: 0.38
    }
  };

  function apiUrl(path, extraParams) {
    const params = new URLSearchParams();
    if (state.workspace) params.set("workspace", state.workspace);
    if (state.database) params.set("database", state.database);
    if (extraParams) {
      for (const [k, v] of Object.entries(extraParams)) {
        if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
      }
    }
    const qs = params.toString();
    return qs ? `${path}?${qs}` : path;
  }

  async function apiFetch(path, { method = "GET", body, headers, params } = {}) {
    const init = {
      method,
      headers: { Accept: "application/json", ...(headers || {}) }
    };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const resp = await fetch(apiUrl(path, params), init);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const error = new Error(data.error || `HTTP ${resp.status}`);
      error.status = resp.status;
      error.details = data.details;
      throw error;
    }
    return data;
  }

  function rebuildSeriesMap() {
    state.seriesMap.clear();
    for (const item of state.settings.series || []) {
      state.seriesMap.set(item.key, item);
    }
  }

  function applyServerPayload(payload, { incremental = false } = {}) {
    if (!payload || typeof payload !== "object") return;
    if (payload.settings) {
      state.settings = payload.settings;
      rebuildSeriesMap();
    }
    if (payload.revision !== undefined) {
      state.revision = payload.revision;
    }
    if (Number.isFinite(payload.server_time_ms)) {
      state.serverOffsetMs = payload.server_time_ms - Date.now();
    }
    const incoming = Array.isArray(payload.samples) ? payload.samples : [];
    if (!incremental) {
      state.samples = incoming;
    } else if (incoming.length) {
      const byTime = new Map(state.samples.map((s) => [s.time_ms, s]));
      for (const item of incoming) byTime.set(item.time_ms, item);
      const retentionMs = (state.settings.retention_seconds || 3600) * 1000;
      const cutoff = (Date.now() + state.serverOffsetMs) - retentionMs - 5000;
      state.samples = [...byTime.values()]
        .filter((s) => s.time_ms >= cutoff)
        .sort((a, b) => a.time_ms - b.time_ms);
      if (state.samples.length > 7200) {
        state.samples = state.samples.slice(state.samples.length - 7200);
      }
    }
    state.lastSampleMs = state.samples.length ? state.samples[state.samples.length - 1].time_ms : 0;
    state.lastSyncMs = Date.now();
    state.error = "";
  }

  function notifyHostSeriesChanged() {
    if (state.host && typeof state.host.onSeriesChange === "function") {
      state.host.onSeriesChange(state.settings.series || []);
    }
    updateTabBadge();
  }

  function updateTabBadge() {
    const badge = el("sample-trends-badge");
    if (!badge) return;
    const count = (state.settings.series || []).length;
    badge.textContent = count > 0 ? String(count) : "";
    badge.hidden = count === 0;
  }

  async function syncFromServer({ configOnly = false, incremental = false } = {}) {
    if (!state.database) {
      state.settings = defaultSettings();
      state.samples = [];
      state.revision = null;
      state.historyLoaded = false;
      rebuildSeriesMap();
      notifyHostSeriesChanged();
      renderAll();
      return;
    }
    if (state.loading) return;
    state.loading = true;
    const targetDb = state.database;
    const targetWs = state.workspace;
    try {
      const afterMs = configOnly
        ? 9999999999999
        : (incremental && state.historyLoaded ? state.lastSampleMs : 0);
      const payload = await apiFetch("/api/manage/sample-trends", {
        params: { after_ms: afterMs }
      });
      if (targetDb !== state.database || targetWs !== state.workspace) return;
      if (configOnly) {
        // 仅同步配置与最新修订号，不覆盖尚未加载的历史样本。
        if (payload.settings) {
          state.settings = payload.settings;
          rebuildSeriesMap();
        }
        if (payload.revision !== undefined) state.revision = payload.revision;
        if (Number.isFinite(payload.server_time_ms)) state.serverOffsetMs = payload.server_time_ms - Date.now();
        state.error = "";
      } else {
        applyServerPayload(payload, { incremental: incremental && state.historyLoaded });
        state.historyLoaded = true;
      }
      notifyHostSeriesChanged();
      renderAll();
    } catch (error) {
      if (targetDb !== state.database || targetWs !== state.workspace) return;
      state.error = error.message || String(error);
      renderStatus();
    } finally {
      state.loading = false;
      scheduleNextPoll();
    }
  }

  async function saveSettings(nextSettings, { silent = false } = {}) {
    if (!state.database) throw new Error(tr("st.noDatabase", null, "请先选择数据库"));
    if (state.saving) return null;
    state.saving = true;
    renderToolbarDisabled();
    try {
      const headers = {};
      if (state.revision) headers["If-Match"] = state.revision;
      const payload = await apiFetch("/api/manage/sample-trends", {
        method: "PUT",
        body: nextSettings,
        headers
      });
      applyServerPayload(payload, { incremental: false });
      state.historyLoaded = true;
      notifyHostSeriesChanged();
      renderAll();
      if (!silent && state.host?.notice) {
        state.host.notice(tr("st.savedNotice", null, "采样点配置已保存"));
      }
      return payload;
    } catch (error) {
      if (error.status === 409 && error.details) {
        applyServerPayload(error.details, { incremental: false });
        state.historyLoaded = true;
        notifyHostSeriesChanged();
        renderAll();
      }
      state.error = error.message || String(error);
      renderStatus();
      if (state.host?.notice) state.host.notice(state.error, true);
      throw error;
    } finally {
      state.saving = false;
      renderToolbarDisabled();
      scheduleNextPoll();
    }
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function scheduleNextPoll() {
    stopPolling();
    if (!state.active || document.hidden || !state.database) return;
    if (!(state.settings.series || []).length) return;
    const intervalMs = Math.max(2000, (state.settings.interval_seconds || 5) * 1000);
    state.pollTimer = setTimeout(() => {
      void syncFromServer({ incremental: true });
    }, intervalMs);
  }

  /* ==================== 布局分栏与拖拽分隔条 ==================== */
  function loadLayout() {
    try {
      const raw = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY) || "null");
      if (raw && typeof raw === "object") {
        if (["split", "chart-full", "list-full"].includes(raw.mode)) state.layout.mode = raw.mode;
        if (typeof raw.listRatio === "number" && raw.listRatio >= 0.16 && raw.listRatio <= 0.80) {
          state.layout.listRatio = raw.listRatio;
        }
      }
    } catch { /* ignore */ }
  }

  function saveLayout() {
    try {
      localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(state.layout));
    } catch { /* ignore */ }
  }

  function applyLayout() {
    const listPane = el("st-list-pane");
    const chartPane = el("st-chart-pane");
    const btnChartFull = el("st-btn-chart-full");
    const btnListFull = el("st-btn-list-full");
    if (!listPane || !chartPane) return;
    const mode = state.layout.mode;
    listPane.classList.toggle("st-pane-hidden", mode === "chart-full");
    chartPane.classList.toggle("st-pane-hidden", mode === "list-full");
    if (mode === "split") {
      listPane.style.flex = `0 0 ${Math.round(state.layout.listRatio * 100)}%`;
      chartPane.style.flex = "1 1 0";
    } else {
      listPane.style.flex = "1 1 0";
      chartPane.style.flex = "1 1 0";
    }
    if (btnChartFull) btnChartFull.classList.toggle("is-active", mode === "chart-full");
    if (btnListFull) btnListFull.classList.toggle("is-active", mode === "list-full");
    requestAnimationFrame(drawChart);
  }

  function bindSplitter() {
    const splitter = el("st-splitter");
    const container = el("tab-sample-trends");
    if (!splitter || !container) return;
    let dragging = null;

    splitter.addEventListener("pointerdown", (event) => {
      if (event.target.closest("button") || event.button !== 0) return;
      event.preventDefault();
      splitter.setPointerCapture(event.pointerId);
      const rect = container.getBoundingClientRect();
      dragging = { pointerId: event.pointerId, top: rect.top, height: Math.max(240, rect.height) };
      splitter.classList.add("is-dragging");
      document.body.classList.add("st-resizing");
    });

    splitter.addEventListener("pointermove", (event) => {
      if (!dragging || event.pointerId !== dragging.pointerId) return;
      const ratio = (event.clientY - dragging.top) / dragging.height;
      state.layout.mode = "split";
      state.layout.listRatio = Math.max(0.18, Math.min(0.76, ratio));
      applyLayout();
    });

    const endDrag = (event) => {
      if (!dragging || event.pointerId !== dragging.pointerId) return;
      splitter.classList.remove("is-dragging");
      document.body.classList.remove("st-resizing");
      if (splitter.hasPointerCapture(dragging.pointerId)) splitter.releasePointerCapture(dragging.pointerId);
      dragging = null;
      saveLayout();
    };
    splitter.addEventListener("pointerup", endDrag);
    splitter.addEventListener("pointercancel", endDrag);

    splitter.addEventListener("dblclick", (event) => {
      if (event.target.closest("button")) return;
      state.layout.mode = "split";
      state.layout.listRatio = 0.38;
      saveLayout();
      applyLayout();
    });

    el("st-btn-chart-full")?.addEventListener("click", () => {
      state.layout.mode = state.layout.mode === "chart-full" ? "split" : "chart-full";
      saveLayout();
      applyLayout();
    });
    el("st-btn-list-full")?.addEventListener("click", () => {
      state.layout.mode = state.layout.mode === "list-full" ? "split" : "list-full";
      saveLayout();
      applyLayout();
    });
  }

  /* ==================== 上半区：采样点定义列表与周期/留存设置 ==================== */
  function latestReadingFor(key) {
    for (let i = state.samples.length - 1; i >= 0; i -= 1) {
      const slot = state.samples[i];
      const item = slot.samples?.[key];
      if (item !== undefined) {
        return { time_ms: slot.time_ms, ...item };
      }
    }
    return null;
  }

  function renderToolbarDisabled() {
    const disabled = !state.database || state.saving;
    for (const id of ["st-add-btn", "st-save-policy", "st-interval", "st-retention-preset", "st-retention-custom"]) {
      const node = el(id);
      if (node) node.disabled = disabled;
    }
    const addBtn = el("st-add-btn");
    if (addBtn && (state.settings.series || []).length >= MAX_POINTS) {
      addBtn.disabled = true;
    }
  }

  function syncPolicyControls() {
    const intervalEl = el("st-interval");
    if (intervalEl) {
      const val = String(state.settings.interval_seconds || 5);
      if (![...intervalEl.options].some((opt) => opt.value === val)) {
        const opt = document.createElement("option");
        opt.value = val;
        opt.textContent = `${val}s`;
        intervalEl.appendChild(opt);
      }
      intervalEl.value = val;
    }
    const retPreset = el("st-retention-preset");
    const retCustom = el("st-retention-custom");
    if (retPreset && retCustom) {
      const mins = Math.max(1, Math.round((state.settings.retention_seconds || 3600) / 60));
      if (RETENTION_PRESETS_MIN.includes(mins)) {
        retPreset.value = String(mins);
        retCustom.hidden = true;
      } else {
        retPreset.value = "custom";
        retCustom.hidden = false;
      }
      retCustom.value = String(mins);
    }
  }

  function syncViewControls() {
    const view = state.settings.view || defaultSettings().view;
    const modeEl = el("st-range-mode");
    const winEl = el("st-window-minutes");
    const stepEl = el("st-step-seconds");
    const fromWrap = el("st-fixed-range-wrap");
    const fromEl = el("st-range-from");
    const untilEl = el("st-range-until");

    if (modeEl) modeEl.value = view.range_mode || "rolling";
    if (winEl) {
      const mins = String(view.window_minutes || 15);
      if (![...winEl.options].some((o) => o.value === mins)) {
        const opt = document.createElement("option");
        opt.value = mins;
        opt.textContent = `${mins} min`;
        winEl.appendChild(opt);
      }
      winEl.value = mins;
      winEl.closest("label").hidden = view.range_mode === "fixed";
    }
    if (stepEl) stepEl.value = String(view.step_seconds || 0);
    if (fromWrap) fromWrap.hidden = view.range_mode !== "fixed";
    if (fromEl && document.activeElement !== fromEl) fromEl.value = view.from || "";
    if (untilEl && document.activeElement !== untilEl) untilEl.value = view.until || "";
  }

  function renderPointList() {
    const tbody = el("st-list-body");
    const countEl = el("st-point-count");
    const series = state.settings.series || [];
    if (countEl) countEl.textContent = `${series.length} / ${MAX_POINTS}`;
    renderToolbarDisabled();
    if (!tbody) return;

    tbody.textContent = "";
    if (!state.database) {
      const trEl = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 8;
      td.className = "st-list-empty";
      td.textContent = tr("st.noDatabase", null, "请先选择数据库");
      trEl.appendChild(td);
      tbody.appendChild(trEl);
      return;
    }

    if (!series.length) {
      const trEl = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 8;
      td.className = "st-list-empty";
      td.textContent = tr("st.emptyList", null, "尚未定义采样点：点击上方“＋ 添加采样点”，或在“结果展示”表格中右键数值/布尔单元格设为采样点。");
      trEl.appendChild(td);
      tbody.appendChild(trEl);
      return;
    }

    const frag = document.createDocumentFragment();
    for (const item of series) {
      const reading = latestReadingFor(item.key);
      const row = document.createElement("tr");
      row.dataset.seriesKey = item.key;
      if (state.focusedKey === item.key) row.classList.add("is-focused");

      // 1. 显隐勾选
      const tdVis = document.createElement("td");
      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.checked = item.visible !== false;
      chk.className = "st-vis-checkbox";
      chk.ariaLabel = item.name;
      chk.addEventListener("change", () => {
        const next = clone(state.settings);
        const target = next.series.find((s) => s.key === item.key);
        if (target) target.visible = chk.checked;
        void saveSettings(next, { silent: true });
      });
      tdVis.appendChild(chk);
      row.appendChild(tdVis);

      // 2. 颜色标识 + 名称
      const tdName = document.createElement("td");
      tdName.className = "st-col-name";
      const dot = document.createElement("span");
      dot.className = "st-color-dot";
      dot.style.background = item.color;
      const nameSpan = document.createElement("span");
      nameSpan.textContent = item.unit ? `${item.name} (${item.unit})` : item.name;
      nameSpan.title = item.name;
      tdName.append(dot, nameSpan);
      row.appendChild(tdName);

      // 3. 表名 · 字段
      const tdTarget = document.createElement("td");
      tdTarget.className = "st-mono";
      tdTarget.textContent = `${item.point.table}.${item.point.field}`;
      row.appendChild(tdTarget);

      // 4. 主键值
      const tdKey = document.createElement("td");
      tdKey.className = "st-mono";
      tdKey.textContent = item.point.key.join(", ");
      row.appendChild(tdKey);

      // 5. 类型（数值 / 布尔）
      const tdKind = document.createElement("td");
      const pill = document.createElement("span");
      pill.className = `st-kind-pill is-${item.kind}`;
      pill.textContent = item.kind === "switch"
        ? tr("st.kindSwitch", null, "布尔")
        : tr("st.kindAnalog", null, "数值");
      tdKind.appendChild(pill);
      row.appendChild(tdKind);

      // 6. 最近采样值 & 时间
      const tdVal = document.createElement("td");
      tdVal.className = "st-num";
      if (!reading) {
        tdVal.textContent = "—";
        tdVal.classList.add("muted");
      } else if (!reading.ok) {
        tdVal.textContent = reading.f
          ? tr("st.invalidVal", null, "无效")
          : tr("st.missingVal", null, "缺失");
        tdVal.classList.add("st-val-warn");
      } else {
        tdVal.textContent = item.unit
          ? `${formatNumber(reading.v, item.kind)} ${item.unit}`
          : formatNumber(reading.v, item.kind);
      }
      row.appendChild(tdVal);

      const tdTime = document.createElement("td");
      tdTime.className = "st-num muted";
      tdTime.textContent = reading ? formatTimeShort(reading.time_ms, false) : "—";
      if (reading) tdTime.title = formatFullTime(reading.time_ms);
      row.appendChild(tdTime);

      // 7. 纵轴选择与操作
      const tdAct = document.createElement("td");
      tdAct.className = "st-col-actions";
      const axisSelect = document.createElement("select");
      axisSelect.className = "st-inline-axis";
      axisSelect.ariaLabel = tr("st.axisLabel", null, "纵轴");
      axisSelect.innerHTML = `<option value="left">${tr("chart.leftAxis", null, "左轴")}</option><option value="right">${tr("chart.rightAxis", null, "右轴")}</option>`;
      axisSelect.value = item.axis || "left";
      axisSelect.addEventListener("change", () => {
        const next = clone(state.settings);
        const target = next.series.find((s) => s.key === item.key);
        if (target) target.axis = axisSelect.value;
        void saveSettings(next, { silent: true });
      });

      const btnFocus = document.createElement("button");
      btnFocus.type = "button";
      btnFocus.className = "secondary small";
      btnFocus.textContent = tr("st.btnLocate", null, "定位");
      btnFocus.addEventListener("click", () => {
        state.focusedKey = state.focusedKey === item.key ? null : item.key;
        if (state.layout.mode === "list-full") {
          state.layout.mode = "split";
          saveLayout();
          applyLayout();
        }
        renderPointList();
        drawChart();
      });

      const btnRemove = document.createElement("button");
      btnRemove.type = "button";
      btnRemove.className = "danger-outline small";
      btnRemove.textContent = tr("st.btnRemove", null, "移除");
      btnRemove.addEventListener("click", () => void confirmAndRemoveSeries(item.key));

      tdAct.append(axisSelect, btnFocus, btnRemove);
      row.appendChild(tdAct);
      frag.appendChild(row);
    }
    tbody.appendChild(frag);
  }

  function renderStatus() {
    const rangeEl = el("st-range-text");
    const statusEl = el("st-status-text");
    const errEl = el("st-error-text");
    const bounds = computeTimeBounds();
    if (rangeEl) {
      const showDate = (bounds.endMs - bounds.startMs) >= 12 * 3600 * 1000;
      rangeEl.textContent = `${formatTimeShort(bounds.startMs, showDate)} ~ ${formatTimeShort(bounds.endMs, showDate)}`;
    }
    if (statusEl) {
      const count = state.samples.length;
      const syncText = state.lastSyncMs ? formatTimeShort(state.lastSyncMs, false) : "—";
      statusEl.textContent = tr(
        "st.statusLine",
        { count, interval: state.settings.interval_seconds || 5, sync: syncText },
        `样本 ${count} 帧 · 周期 ${state.settings.interval_seconds || 5}s · 更新 ${syncText}`
      );
    }
    if (errEl) {
      errEl.textContent = state.error || "";
      errEl.hidden = !state.error;
    }
  }

  function renderAll() {
    syncPolicyControls();
    syncViewControls();
    renderPointList();
    renderStatus();
    if (state.active) requestAnimationFrame(drawChart);
  }

  /* ==================== 时间窗口与采样对齐 ==================== */
  function computeTimeBounds() {
    const view = state.settings.view || defaultSettings().view;
    const nowMs = Date.now() + state.serverOffsetMs;
    const winMs = Math.max(1, view.window_minutes || 15) * 60 * 1000;
    if (view.range_mode === "fixed") {
      const fromMs = parseDateTimeInput(view.from);
      const untilMs = parseDateTimeInput(view.until);
      if (fromMs !== null && untilMs !== null && untilMs > fromMs) {
        return { startMs: fromMs, endMs: untilMs };
      }
    }
    return { startMs: nowMs - winMs, endMs: nowMs };
  }

  function alignedSamples(startMs, endMs) {
    const stepSec = (state.settings.view && state.settings.view.step_seconds) || 0;
    const inRange = state.samples.filter((s) => s.time_ms >= startMs - 60000 && s.time_ms <= endMs + 60000);
    if (!stepSec || !inRange.length) return inRange;
    const stepMs = stepSec * 1000;
    const buckets = new Map();
    for (const slot of inRange) {
      const bucket = Math.floor(slot.time_ms / stepMs) * stepMs;
      buckets.set(bucket, { time_ms: bucket, samples: slot.samples });
    }
    return [...buckets.values()].sort((a, b) => a.time_ms - b.time_ms);
  }

  /* ==================== Canvas 双轴曲线板绘制 ==================== */
  function computeAxisBounds(seriesList, slots, axisName, startMs, endMs) {
    const viewAxes = state.settings.view?.axes || {};
    const cfg = viewAxes[axisName] || { auto: true, min: null, max: null, unit: "" };
    if (!cfg.auto && cfg.min !== null && cfg.max !== null && cfg.max > cfg.min) {
      return { min: cfg.min, max: cfg.max, unit: cfg.unit || "" };
    }
    let minVal = Infinity;
    let maxVal = -Infinity;
    let allSwitch = seriesList.length > 0;
    for (const item of seriesList) {
      if (item.kind !== "switch") allSwitch = false;
      for (const slot of slots) {
        if (slot.time_ms < startMs || slot.time_ms > endMs) continue;
        const point = slot.samples?.[item.key];
        if (!point || !point.ok || !Number.isFinite(point.v)) continue;
        if (point.v < minVal) minVal = point.v;
        if (point.v > maxVal) maxVal = point.v;
      }
    }
    if (!Number.isFinite(minVal) || !Number.isFinite(maxVal)) {
      return allSwitch
        ? { min: -0.1, max: 1.1, unit: cfg.unit || "" }
        : { min: 0, max: 100, unit: cfg.unit || "" };
    }
    if (allSwitch) {
      return { min: -0.1, max: 1.15, unit: cfg.unit || "" };
    }
    if (minVal === maxVal) {
      const pad = minVal === 0 ? 1 : Math.abs(minVal) * 0.12;
      minVal -= pad;
      maxVal += pad;
    } else {
      const pad = (maxVal - minVal) * 0.08;
      minVal -= pad;
      maxVal += pad;
    }
    if (!cfg.auto && cfg.min !== null) minVal = cfg.min;
    if (!cfg.auto && cfg.max !== null) maxVal = cfg.max;
    if (maxVal <= minVal) maxVal = minVal + 1;
    return { min: minVal, max: maxVal, unit: cfg.unit || inferUnit(seriesList) };
  }

  function inferUnit(seriesList) {
    const units = new Set(seriesList.map((s) => s.unit).filter(Boolean));
    return units.size === 1 ? [...units][0] : "";
  }

  function drawChart() {
    const canvas = el("st-canvas");
    const wrap = el("st-chart-wrap");
    const emptyEl = el("st-chart-empty");
    if (!canvas || !wrap) return;

    const rect = wrap.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 20) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.floor(rect.width);
    const height = Math.floor(rect.height);
    if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
    }

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const tokens = themeTokens();

    ctx.fillStyle = tokens.bg;
    ctx.fillRect(0, 0, width, height);

    const allSeries = state.settings.series || [];
    state.legendHits = [];

    if (!allSeries.length) {
      if (emptyEl) {
        emptyEl.textContent = tr("st.chartEmpty", null, "添加采样点后，后台将按设定周期自动记录并在此实时绘制双轴趋势曲线。");
        emptyEl.hidden = false;
      }
      hideTooltip();
      return;
    }

    // 1. 绘制顶部流式图例条（参考 SCADA point-trend.js：点击切显隐、右键单曲线菜单）
    const legendTop = 8;
    let legendX = 12;
    let legendY = legendTop;
    const legendRowH = 20;
    const maxLegendY = legendTop + legendRowH; // 最多 2 行，超出折叠为 +N
    ctx.font = `12px ${tokens.fontSans}`;
    ctx.textBaseline = "middle";
    let hiddenOverflow = 0;

    for (let i = 0; i < allSeries.length; i += 1) {
      const s = allSeries[i];
      const axisTag = s.axis === "right" ? tr("chart.axisRightShort", null, "右") : tr("chart.axisLeftShort", null, "左");
      const label = `[${axisTag}] ${s.name}`;
      const textW = Math.min(180, ctx.measureText(label).width);
      const pillW = 24 + textW + 10;
      if (legendX + pillW > width - 48) {
        if (legendY + legendRowH <= maxLegendY) {
          legendY += legendRowH + 4;
          legendX = 12;
        } else {
          hiddenOverflow = allSeries.length - i;
          break;
        }
      }
      const isFocused = state.focusedKey === s.key;
      const visible = s.visible !== false;

      ctx.save();
      ctx.globalAlpha = visible ? 1 : 0.4;
      ctx.fillStyle = isFocused ? `${s.color}24` : (tokens.dark ? "rgba(255,255,255,0.05)" : "rgba(15,23,42,0.04)");
      ctx.strokeStyle = isFocused ? s.color : tokens.border;
      ctx.lineWidth = isFocused ? 1.5 : 1;
      roundRect(ctx, legendX, legendY, pillW, legendRowH, 5);
      ctx.fill();
      ctx.stroke();

      // 图标：开关量画阶跃线，模拟量画直线+圆点
      ctx.strokeStyle = s.color;
      ctx.fillStyle = s.color;
      ctx.lineWidth = 2;
      const cy = legendY + legendRowH / 2;
      ctx.beginPath();
      if (s.kind === "switch") {
        ctx.moveTo(legendX + 6, cy + 4);
        ctx.lineTo(legendX + 11, cy + 4);
        ctx.lineTo(legendX + 11, cy - 4);
        ctx.lineTo(legendX + 18, cy - 4);
        ctx.stroke();
      } else {
        ctx.moveTo(legendX + 6, cy);
        ctx.lineTo(legendX + 18, cy);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(legendX + 12, cy, 2.6, 0, Math.PI * 2);
        ctx.fill();
      }

      ctx.fillStyle = tokens.text;
      ctx.fillText(truncateText(ctx, label, textW), legendX + 23, cy + 0.5);
      ctx.restore();

      state.legendHits.push({ key: s.key, x: legendX, y: legendY, w: pillW, h: legendRowH });
      legendX += pillW + 6;
    }

    if (hiddenOverflow > 0) {
      const moreText = `+${hiddenOverflow}`;
      const moreW = ctx.measureText(moreText).width + 14;
      const mx = Math.min(legendX, width - moreW - 10);
      ctx.fillStyle = tokens.surface;
      ctx.strokeStyle = tokens.border;
      roundRect(ctx, mx, legendY, moreW, legendRowH, 5);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = tokens.textSecondary;
      ctx.fillText(moreText, mx + 7, legendY + legendRowH / 2 + 0.5);
      state.legendHits.push({ key: null, overflow: true, x: mx, y: legendY, w: moreW, h: legendRowH });
    }

    const visibleSeries = allSeries.filter((s) => s.visible !== false);
    const leftSeries = visibleSeries.filter((s) => s.axis !== "right");
    const rightSeries = visibleSeries.filter((s) => s.axis === "right");

    const padTop = legendY + legendRowH + 14;
    const padBottom = 30;
    const padLeft = leftSeries.length ? 62 : 20;
    const padRight = rightSeries.length ? 62 : 20;
    const plotW = Math.max(40, width - padLeft - padRight);
    const plotH = Math.max(40, height - padTop - padBottom);

    const { startMs, endMs } = computeTimeBounds();
    const slots = alignedSamples(startMs, endMs);

    if (!visibleSeries.length) {
      if (emptyEl) {
        emptyEl.textContent = tr("chart.allHidden", null, "全部曲线已隐藏，点击顶部图例恢复显示。");
        emptyEl.hidden = false;
      }
      hideTooltip();
      return;
    }
    if (emptyEl) emptyEl.hidden = true;

    const leftBounds = leftSeries.length ? computeAxisBounds(leftSeries, slots, "left", startMs, endMs) : null;
    const rightBounds = rightSeries.length ? computeAxisBounds(rightSeries, slots, "right", startMs, endMs) : null;

    // 2. 绘制网格与坐标轴边框
    ctx.strokeStyle = tokens.grid;
    ctx.lineWidth = 1;

    const yTickCount = Math.max(3, Math.min(6, Math.floor(plotH / 42)));
    const refBounds = leftBounds || rightBounds;
    const refTicks = niceTicks(refBounds.min, refBounds.max, yTickCount);
    const yMap = (val, b) => padTop + plotH - ((val - b.min) / (b.max - b.min)) * plotH;
    const xMap = (ms) => padLeft + ((ms - startMs) / Math.max(1, endMs - startMs)) * plotW;

    ctx.font = `11px ${tokens.fontMono}`;
    ctx.textBaseline = "middle";

    // 水平网格线 & 左轴刻度
    if (leftBounds) {
      const lTicks = niceTicks(leftBounds.min, leftBounds.max, yTickCount);
      ctx.textAlign = "right";
      for (const tick of lTicks) {
        const y = yMap(tick, leftBounds);
        if (y < padTop - 2 || y > padTop + plotH + 2) continue;
        ctx.beginPath();
        ctx.strokeStyle = tokens.grid;
        ctx.moveTo(padLeft, Math.round(y) + 0.5);
        ctx.lineTo(padLeft + plotW, Math.round(y) + 0.5);
        ctx.stroke();

        ctx.fillStyle = tokens.muted;
        ctx.fillText(formatNumber(tick, "analog"), padLeft - 7, y);
      }
      if (leftBounds.unit) {
        ctx.fillStyle = tokens.textSecondary;
        ctx.textAlign = "left";
        ctx.fillText(leftBounds.unit, 8, padTop - 8);
      }
    } else {
      for (const tick of refTicks) {
        const y = yMap(tick, refBounds);
        if (y < padTop - 2 || y > padTop + plotH + 2) continue;
        ctx.beginPath();
        ctx.strokeStyle = tokens.grid;
        ctx.moveTo(padLeft, Math.round(y) + 0.5);
        ctx.lineTo(padLeft + plotW, Math.round(y) + 0.5);
        ctx.stroke();
      }
    }

    // 右轴刻度
    if (rightBounds) {
      const rTicks = niceTicks(rightBounds.min, rightBounds.max, yTickCount);
      ctx.textAlign = "left";
      for (const tick of rTicks) {
        const y = yMap(tick, rightBounds);
        if (y < padTop - 2 || y > padTop + plotH + 2) continue;
        ctx.fillStyle = tokens.muted;
        ctx.fillText(formatNumber(tick, "analog"), padLeft + plotW + 7, y);
      }
      if (rightBounds.unit) {
        ctx.fillStyle = tokens.textSecondary;
        ctx.textAlign = "right";
        ctx.fillText(rightBounds.unit, width - 8, padTop - 8);
      }
    }

    // 垂直时间网格线 & X 轴刻度（防重叠）
    const spanMs = Math.max(1000, endMs - startMs);
    const showDate = spanMs >= 12 * 3600 * 1000;
    const minLabelPx = showDate ? 112 : 76;
    const maxXTicks = Math.max(2, Math.floor(plotW / minLabelPx));
    const xStepMs = chooseTimeStep(spanMs, maxXTicks);
    const firstTick = Math.ceil(startMs / xStepMs) * xStepMs;

    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    let lastLabelRight = -Infinity;
    for (let t = firstTick; t <= endMs; t += xStepMs) {
      const x = xMap(t);
      ctx.beginPath();
      ctx.strokeStyle = tokens.grid;
      ctx.moveTo(Math.round(x) + 0.5, padTop);
      ctx.lineTo(Math.round(x) + 0.5, padTop + plotH);
      ctx.stroke();

      const label = formatTimeShort(t, showDate);
      const labelW = ctx.measureText(label).width;
      if (x - labelW / 2 >= lastLabelRight + 6) {
        ctx.fillStyle = tokens.muted;
        ctx.fillText(label, x, padTop + plotH + 7);
        lastLabelRight = x + labelW / 2;
      }
    }

    // 绘图区外框
    ctx.strokeStyle = tokens.border;
    ctx.strokeRect(padLeft + 0.5, padTop + 0.5, plotW, plotH);

    // 3. 裁剪绘图区并逐条绘制曲线（断采断线 + 开关量阶跃线 + 孤立单点圆点）
    const gapLimitMs = Math.max((state.settings.interval_seconds || 5) * 3500, 15000);
    ctx.save();
    ctx.beginPath();
    ctx.rect(padLeft, padTop, plotW, plotH);
    ctx.clip();

    for (const s of visibleSeries) {
      const bounds = s.axis === "right" ? rightBounds : leftBounds;
      if (!bounds) continue;
      const isFocused = state.focusedKey === s.key;
      const pts = [];
      for (const slot of slots) {
        const rec = slot.samples?.[s.key];
        if (!rec || !rec.ok || !Number.isFinite(rec.v)) {
          pts.push(null);
          continue;
        }
        pts.push({ t: slot.time_ms, x: xMap(slot.time_ms), y: yMap(rec.v, bounds), v: rec.v });
      }

      ctx.strokeStyle = s.color;
      ctx.fillStyle = s.color;
      ctx.lineWidth = isFocused ? Math.min(6, (s.width || 2) + 1.5) : (s.width || 2);
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      if (isFocused) {
        ctx.shadowColor = s.color;
        ctx.shadowBlur = 6;
      } else {
        ctx.shadowBlur = 0;
      }

      let prev = null;
      let segmentLen = 0;
      ctx.beginPath();
      for (let i = 0; i < pts.length; i += 1) {
        const cur = pts[i];
        if (!cur) {
          if (segmentLen === 1 && prev) drawDot(ctx, prev.x, prev.y, Math.max(3, ctx.lineWidth + 1));
          ctx.stroke();
          ctx.beginPath();
          prev = null;
          segmentLen = 0;
          continue;
        }
        if (!prev || (cur.t - prev.t) > gapLimitMs) {
          if (segmentLen === 1 && prev) drawDot(ctx, prev.x, prev.y, Math.max(3, ctx.lineWidth + 1));
          ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(cur.x, cur.y);
          segmentLen = 1;
        } else {
          if (s.kind === "switch") {
            ctx.lineTo(cur.x, prev.y);
            ctx.lineTo(cur.x, cur.y);
          } else {
            ctx.lineTo(cur.x, cur.y);
          }
          segmentLen += 1;
        }
        prev = cur;
      }
      ctx.stroke();
      if (segmentLen === 1 && prev) {
        drawDot(ctx, prev.x, prev.y, Math.max(3, ctx.lineWidth + 1));
      }
      ctx.shadowBlur = 0;
    }

    // 4. 悬停十字准星与命中点高亮
    let tooltipLines = null;
    if (state.hover && state.hover.x >= padLeft && state.hover.x <= padLeft + plotW
        && state.hover.y >= padTop && state.hover.y <= padTop + plotH) {
      const hoverMs = startMs + ((state.hover.x - padLeft) / plotW) * (endMs - startMs);
      const nearest = findNearestSlot(slots, hoverMs, gapLimitMs);
      const cursorX = nearest ? xMap(nearest.time_ms) : state.hover.x;

      ctx.strokeStyle = tokens.muted;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(Math.round(cursorX) + 0.5, padTop);
      ctx.lineTo(Math.round(cursorX) + 0.5, padTop + plotH);
      ctx.stroke();
      ctx.setLineDash([]);

      const timeStr = formatFullTime(nearest ? nearest.time_ms : hoverMs);
      tooltipLines = [timeStr];
      if (nearest) {
        for (const s of visibleSeries) {
          const rec = nearest.samples?.[s.key];
          const bounds = s.axis === "right" ? rightBounds : leftBounds;
          let valText = "—";
          if (rec && rec.ok && Number.isFinite(rec.v)) {
            valText = s.unit ? `${formatNumber(rec.v, s.kind)} ${s.unit}` : formatNumber(rec.v, s.kind);
            if (bounds) {
              const py = yMap(rec.v, bounds);
              ctx.fillStyle = tokens.surface;
              ctx.strokeStyle = s.color;
              ctx.lineWidth = 2;
              ctx.beginPath();
              ctx.arc(cursorX, py, 4, 0, Math.PI * 2);
              ctx.fill();
              ctx.stroke();
            }
          } else if (rec && !rec.ok) {
            valText = rec.f ? tr("st.invalidVal", null, "无效") : tr("st.missingVal", null, "缺失");
          }
          tooltipLines.push(`${s.name}: ${valText}`);
        }
      }
    }

    ctx.restore();

    if (tooltipLines) {
      showTooltip(tooltipLines, state.hover.x > padLeft + plotW * 0.55);
    } else {
      hideTooltip();
    }
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function truncateText(ctx, text, maxW) {
    if (ctx.measureText(text).width <= maxW) return text;
    let out = text;
    while (out.length > 2 && ctx.measureText(out + "…").width > maxW) {
      out = out.slice(0, -1);
    }
    return out + "…";
  }

  function drawDot(ctx, x, y, r) {
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }

  function chooseTimeStep(spanMs, maxTicks) {
    const candidates = [
      1000, 2000, 5000, 10000, 15000, 30000,
      60000, 120000, 300000, 600000, 900000, 1800000,
      3600000, 7200000, 14400000, 21600000, 43200000, 86400000
    ];
    const rough = spanMs / Math.max(2, maxTicks);
    for (const c of candidates) {
      if (c >= rough) return c;
    }
    return candidates[candidates.length - 1];
  }

  function findNearestSlot(slots, targetMs, maxDistMs) {
    if (!slots.length) return null;
    let lo = 0;
    let hi = slots.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (slots[mid].time_ms < targetMs) lo = mid + 1;
      else hi = mid;
    }
    let best = slots[lo];
    if (lo > 0 && Math.abs(slots[lo - 1].time_ms - targetMs) < Math.abs(best.time_ms - targetMs)) {
      best = slots[lo - 1];
    }
    return Math.abs(best.time_ms - targetMs) <= Math.max(maxDistMs, 60000) ? best : null;
  }

  function showTooltip(lines, alignLeft) {
    const tip = el("st-chart-tooltip");
    if (!tip) return;
    tip.textContent = lines.join("\n");
    tip.classList.toggle("is-left", Boolean(alignLeft));
    tip.hidden = false;
  }

  function hideTooltip() {
    const tip = el("st-chart-tooltip");
    if (tip) tip.hidden = true;
  }

  /* ==================== 缩放、重置与 CSV 导出 ==================== */
  function zoomWindow(factor) {
    const next = clone(state.settings);
    const view = next.view;
    if (view.range_mode === "fixed") {
      const { startMs, endMs } = computeTimeBounds();
      const center = (startMs + endMs) / 2;
      const half = Math.max(30000, Math.min(12 * 3600 * 1000, ((endMs - startMs) / 2) * factor));
      view.from = formatDateTimeInput(center - half);
      view.until = formatDateTimeInput(center + half);
    } else {
      const cur = view.window_minutes || 15;
      const nextMin = Math.max(1, Math.min(1440, Math.round(cur * factor)));
      view.window_minutes = nextMin;
    }
    state.settings = next;
    syncViewControls();
    renderStatus();
    drawChart();
    clearTimeout(state.wheelTimer);
    state.wheelTimer = setTimeout(() => {
      void saveSettings(state.settings, { silent: true });
    }, 350);
  }

  function resetView() {
    const next = clone(state.settings);
    next.view.range_mode = "rolling";
    next.view.window_minutes = 15;
    next.view.step_seconds = 0;
    next.view.from = "";
    next.view.until = "";
    next.view.axes.left.auto = true;
    next.view.axes.right.auto = true;
    state.focusedKey = null;
    void saveSettings(next, { silent: true });
  }

  function exportCsv() {
    const series = state.settings.series || [];
    if (!series.length) {
      if (state.host?.notice) state.host.notice(tr("st.noDataExport", null, "暂无采样点可导出"), true);
      return;
    }
    const { startMs, endMs } = computeTimeBounds();
    const slots = alignedSamples(startMs, endMs).filter((s) => s.time_ms >= startMs && s.time_ms <= endMs);
    const escapeCsv = (val) => {
      const text = val === null || val === undefined ? "" : String(val);
      return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    };
    const header = ["timestamp", "time_ms", ...series.map((s) => s.unit ? `${s.name} (${s.unit})` : s.name)];
    const rows = [header.map(escapeCsv).join(",")];
    for (const slot of slots) {
      const cols = [formatFullTime(slot.time_ms), slot.time_ms];
      for (const s of series) {
        const rec = slot.samples?.[s.key];
        cols.push(rec && rec.ok && Number.isFinite(rec.v) ? rec.v : "");
      }
      rows.push(cols.map(escapeCsv).join(","));
    }
    const blob = new Blob(["\uFEFF" + rows.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const dbStem = (state.database || "rtdb").replace(/\.[^.]+$/, "");
    a.download = `${dbStem}_sample_trends_${Date.now()}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  /* ==================== 增删改采样点（含模态确认与弹窗） ==================== */
  function pickNextColor(seriesList) {
    const used = new Set(seriesList.map((s) => s.color));
    for (const color of DEFAULT_PALETTE) {
      if (!used.has(color)) return color;
    }
    return DEFAULT_PALETTE[seriesList.length % DEFAULT_PALETTE.length];
  }

  async function addOrUpdateFromCell({ table, key, field, name, unit = "", kind = "analog", axis = "left" }) {
    if (!state.database) throw new Error(tr("st.noDatabase", null, "请先选择数据库"));
    const pkey = pointKey(table, key, field);
    const next = clone(state.settings);
    const existing = next.series.find((s) => s.key === pkey);
    if (existing) {
      existing.axis = axis;
      existing.visible = true;
      await saveSettings(next);
      return existing;
    }
    if (next.series.length >= MAX_POINTS) {
      throw new Error(tr("st.limitReached", { max: MAX_POINTS }, `最多支持定义 ${MAX_POINTS} 个采样点`));
    }
    const item = {
      key: pkey,
      point: { table, key, field },
      name: name || `${table}[${key.join(",")}].${field}`,
      unit: unit || "",
      kind: kind === "switch" ? "switch" : "analog",
      visible: true,
      axis: axis === "right" ? "right" : "left",
      color: pickNextColor(next.series),
      width: 2
    };
    next.series.push(item);
    await saveSettings(next);
    return item;
  }

  async function toggleAxisForCell(table, key, field) {
    const pkey = pointKey(table, key, field);
    const next = clone(state.settings);
    const existing = next.series.find((s) => s.key === pkey);
    if (!existing) return;
    existing.axis = existing.axis === "right" ? "left" : "right";
    await saveSettings(next);
  }

  async function confirmAndRemoveSeries(pkey) {
    const item = state.seriesMap.get(pkey);
    if (!item) return;
    const title = tr("st.removeConfirmTitle", null, "移除采样点");
    const msg = tr("st.removeConfirmMsg", { name: item.name }, `确定移除采样点「${item.name}」？移除后后台将停止对该点采集。`);
    const ok = state.host?.confirmAction
      ? await state.host.confirmAction(title, msg)
      : window.confirm(msg);
    if (!ok) return;
    const next = clone(state.settings);
    next.series = next.series.filter((s) => s.key !== pkey);
    if (state.focusedKey === pkey) state.focusedKey = null;
    await saveSettings(next);
  }

  async function removeFromCell(table, key, field) {
    await confirmAndRemoveSeries(pointKey(table, key, field));
  }

  function focusPoint(table, key, field) {
    const pkey = pointKey(table, key, field);
    state.focusedKey = pkey;
    if (state.layout.mode === "list-full") {
      state.layout.mode = "split";
      saveLayout();
    }
    if (state.host?.switchToSampleTab) state.host.switchToSampleTab();
    renderPointList();
    drawChart();
  }

  /* ==================== 「＋ 添加采样点」模态对话框（全下拉选择） ==================== */
  async function openAddDialog() {
    const dlg = el("st-add-dialog");
    const tableSel = el("st-add-table");
    const rowSel = el("st-add-row");
    const fieldSel = el("st-add-field");
    const axisSel = el("st-add-axis");
    const nameInput = el("st-add-name");
    const unitInput = el("st-add-unit");
    const errEl = el("st-add-error");
    if (!dlg || !tableSel || !rowSel || !fieldSel) return;

    errEl.textContent = "";
    unitInput.value = "";
    axisSel.value = "left";
    const tables = (state.host?.getTables ? state.host.getTables() : []) || [];
    state.addDialog.tables = tables;
    tableSel.textContent = "";
    if (!tables.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = tr("st.noTables", null, "当前数据库暂无表");
      tableSel.appendChild(opt);
      rowSel.textContent = "";
      fieldSel.textContent = "";
      el("st-add-confirm").disabled = true;
      dlg.showModal();
      return;
    }

    for (const t of tables) {
      const opt = document.createElement("option");
      opt.value = t.name;
      opt.textContent = t.label && t.label !== t.name ? `${t.label} (${t.name})` : t.name;
      tableSel.appendChild(opt);
    }
    const activeTable = state.host?.getCurrentTable ? state.host.getCurrentTable() : "";
    if (activeTable && tables.some((t) => t.name === activeTable)) {
      tableSel.value = activeTable;
    }
    dlg.showModal();
    await loadAddDialogTable(tableSel.value);
  }

  async function loadAddDialogTable(tableName) {
    const rowSel = el("st-add-row");
    const fieldSel = el("st-add-field");
    const errEl = el("st-add-error");
    const confirmBtn = el("st-add-confirm");
    if (!tableName || !state.host) return;
    errEl.textContent = "";
    rowSel.innerHTML = `<option value="">${tr("st.loading", null, "加载中…")}</option>`;
    fieldSel.innerHTML = `<option value="">${tr("st.loading", null, "加载中…")}</option>`;
    confirmBtn.disabled = true;

    try {
      const [schemaResp, rowsResp] = await Promise.all([
        state.host.fetchTableSchema(tableName),
        state.host.fetchTableRows(tableName, 200)
      ]);
      const def = schemaResp.definition || schemaResp;
      const columns = def.columns || [];
      const pkCols = columns.filter((c) => c.primary_key);
      const sampleCols = columns.filter((c) => isSampleableType(c.data_type));
      const records = rowsResp.records || [];

      state.addDialog.schema = def;
      state.addDialog.rows = records;

      rowSel.textContent = "";
      if (!records.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = tr("st.noRows", null, "该表暂无数据行");
        rowSel.appendChild(opt);
      } else {
        const nameCol = columns.find((c) => !c.primary_key && /name|desc|label|title/i.test(c.eng_name));
        records.forEach((rec, idx) => {
          const opt = document.createElement("option");
          opt.value = String(idx);
          const pkText = (rec.ref?.key || pkCols.map((c) => rec.row?.[c.eng_name])).join(", ");
          const desc = nameCol && rec.row?.[nameCol.eng_name] ? ` · ${rec.row[nameCol.eng_name]}` : "";
          opt.textContent = `[${pkText}]${desc}`;
          rowSel.appendChild(opt);
        });
      }

      fieldSel.textContent = "";
      if (!sampleCols.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = tr("st.noNumericFields", null, "该表无数值或布尔字段");
        fieldSel.appendChild(opt);
      } else {
        for (const col of sampleCols) {
          const opt = document.createElement("option");
          opt.value = col.eng_name;
          const label = col.chn_name && col.chn_name !== col.eng_name ? `${col.chn_name} (${col.eng_name})` : col.eng_name;
          opt.textContent = `${label} · ${col.data_type}`;
          fieldSel.appendChild(opt);
        }
      }

      confirmBtn.disabled = !(records.length && sampleCols.length);
      updateAddDialogDefaultName();
    } catch (error) {
      errEl.textContent = error.message || String(error);
      confirmBtn.disabled = true;
    }
  }

  function updateAddDialogDefaultName() {
    const tableSel = el("st-add-table");
    const rowSel = el("st-add-row");
    const fieldSel = el("st-add-field");
    const nameInput = el("st-add-name");
    if (!tableSel || !rowSel || !fieldSel || !nameInput) return;
    const rec = state.addDialog.rows[Number(rowSel.value)];
    const def = state.addDialog.schema;
    if (!rec || !def || !fieldSel.value) return;
    const tableLabel = def.head?.chn_name || def.head?.eng_name || tableSel.value;
    const col = (def.columns || []).find((c) => c.eng_name === fieldSel.value);
    const colLabel = col?.chn_name || fieldSel.value;
    const keyText = (rec.ref?.key || []).join(",");
    nameInput.value = `${tableLabel} #${keyText} · ${colLabel}`;
  }

  /* ==================== 纵轴设置弹窗 & 曲线设置弹窗 ==================== */
  function openAxesDialog() {
    const dlg = el("st-axes-dialog");
    if (!dlg) return;
    const axes = state.settings.view?.axes || defaultSettings().view.axes;
    for (const side of ["left", "right"]) {
      const cfg = axes[side] || { auto: true, min: null, max: null, unit: "" };
      const autoEl = el(`st-axis-${side}-auto`);
      const minEl = el(`st-axis-${side}-min`);
      const maxEl = el(`st-axis-${side}-max`);
      const unitEl = el(`st-axis-${side}-unit`);
      if (autoEl) autoEl.checked = cfg.auto !== false;
      if (minEl) { minEl.value = cfg.min ?? ""; minEl.disabled = cfg.auto !== false; }
      if (maxEl) { maxEl.value = cfg.max ?? ""; maxEl.disabled = cfg.auto !== false; }
      if (unitEl) unitEl.value = cfg.unit || "";
    }
    el("st-axes-error").textContent = "";
    dlg.showModal();
  }

  function openSeriesDialog() {
    const dlg = el("st-series-dialog");
    const body = el("st-series-dialog-body");
    if (!dlg || !body) return;
    body.textContent = "";
    const series = state.settings.series || [];
    if (!series.length) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = tr("st.emptyListShort", null, "当前没有已定义的采样点。");
      body.appendChild(p);
    } else {
      for (const item of series) {
        const row = document.createElement("div");
        row.className = "st-series-edit-row";
        row.dataset.seriesKey = item.key;

        const vis = document.createElement("input");
        vis.type = "checkbox";
        vis.className = "st-ed-vis";
        vis.checked = item.visible !== false;

        const color = document.createElement("input");
        color.type = "color";
        color.className = "st-ed-color";
        color.value = item.color || DEFAULT_PALETTE[0];

        const name = document.createElement("input");
        name.type = "text";
        name.className = "st-ed-name";
        name.value = item.name;
        name.maxLength = 120;
        name.placeholder = tr("st.colName", null, "名称");

        const unit = document.createElement("input");
        unit.type = "text";
        unit.className = "st-ed-unit";
        unit.value = item.unit || "";
        unit.maxLength = 24;
        unit.placeholder = tr("st.colUnit", null, "单位");

        const axis = document.createElement("select");
        axis.className = "st-ed-axis";
        axis.innerHTML = `<option value="left">${tr("chart.leftAxis", null, "左轴")}</option><option value="right">${tr("chart.rightAxis", null, "右轴")}</option>`;
        axis.value = item.axis || "left";

        const width = document.createElement("select");
        width.className = "st-ed-width";
        for (let w = 1; w <= 5; w += 1) {
          const opt = document.createElement("option");
          opt.value = String(w);
          opt.textContent = `${w}px`;
          width.appendChild(opt);
        }
        width.value = String(item.width || 2);

        row.append(vis, color, name, unit, axis, width);
        body.appendChild(row);
      }
    }
    el("st-series-error").textContent = "";
    dlg.showModal();
  }

  /* ==================== 曲线区右键菜单 ==================== */
  function closeChartMenu() {
    const menu = el("st-chart-context-menu");
    if (menu) menu.hidden = true;
    state.contextSeriesKey = null;
  }

  function openChartMenu(event, hitKey) {
    event.preventDefault();
    event.stopPropagation();
    const menu = el("st-chart-context-menu");
    if (!menu) return;
    state.contextSeriesKey = hitKey || null;
    const item = hitKey ? state.seriesMap.get(hitKey) : null;
    const seriesSection = el("st-cm-series-group");
    if (seriesSection) seriesSection.hidden = !item;
    if (item) {
      const btnAxis = menu.querySelector('[data-st-action="switch-axis"]');
      const btnVis = menu.querySelector('[data-st-action="toggle-visible"]');
      if (btnAxis) {
        btnAxis.textContent = item.axis === "right"
          ? tr("st.cmToLeft", null, "切换至左轴")
          : tr("st.cmToRight", null, "切换至右轴");
      }
      if (btnVis) {
        btnVis.textContent = item.visible === false
          ? tr("st.cmShow", null, "显示此曲线")
          : tr("st.cmHide", null, "隐藏此曲线");
      }
    }
    menu.hidden = false;
    const margin = 8;
    const left = Math.max(margin, Math.min(event.clientX, window.innerWidth - menu.offsetWidth - margin));
    const top = Math.max(margin, Math.min(event.clientY, window.innerHeight - menu.offsetHeight - margin));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
  }

  function hitTestLegend(canvas, clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;
    for (const box of state.legendHits) {
      if (x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h) {
        return box;
      }
    }
    return null;
  }

  /* ==================== 事件绑定 ==================== */
  function bindEvents() {
    bindSplitter();

    // 留存预设切换
    el("st-retention-preset")?.addEventListener("change", (e) => {
      const custom = el("st-retention-custom");
      if (!custom) return;
      if (e.target.value === "custom") {
        custom.hidden = false;
        custom.focus();
      } else {
        custom.hidden = true;
        custom.value = e.target.value;
      }
    });

    // 保存采样周期与留存时长
    el("st-save-policy")?.addEventListener("click", () => {
      const intervalSec = Number(el("st-interval")?.value || 5);
      const preset = el("st-retention-preset")?.value;
      const mins = preset === "custom" ? Number(el("st-retention-custom")?.value || 60) : Number(preset || 60);
      if (!Number.isFinite(mins) || mins < 1 || mins > 1440) {
        if (state.host?.notice) state.host.notice(tr("st.retentionRangeErr", null, "留存时长须在 1–1440 分钟之间"), true);
        return;
      }
      const next = clone(state.settings);
      next.interval_seconds = Math.max(1, Math.min(60, Math.round(intervalSec)));
      next.retention_seconds = Math.round(mins * 60);
      void saveSettings(next);
    });

    // 添加采样点弹窗
    el("st-add-btn")?.addEventListener("click", () => void openAddDialog());
    el("st-add-table")?.addEventListener("change", (e) => void loadAddDialogTable(e.target.value));
    el("st-add-row")?.addEventListener("change", updateAddDialogDefaultName);
    el("st-add-field")?.addEventListener("change", updateAddDialogDefaultName);
    el("st-add-cancel")?.addEventListener("click", () => el("st-add-dialog")?.close());
    el("st-add-close-x")?.addEventListener("click", () => el("st-add-dialog")?.close());
    el("st-add-confirm")?.addEventListener("click", async () => {
      const table = el("st-add-table")?.value;
      const rowIdx = Number(el("st-add-row")?.value);
      const field = el("st-add-field")?.value;
      const axis = el("st-add-axis")?.value || "left";
      const name = (el("st-add-name")?.value || "").trim();
      const unit = (el("st-add-unit")?.value || "").trim();
      const errEl = el("st-add-error");
      const rec = state.addDialog.rows[rowIdx];
      const def = state.addDialog.schema;
      if (!table || !rec || !field || !def) {
        errEl.textContent = tr("st.selectComplete", null, "请选择完整的表、行和字段");
        return;
      }
      const col = (def.columns || []).find((c) => c.eng_name === field);
      const key = rec.ref?.key || (def.columns || []).filter((c) => c.primary_key).map((c) => rec.row?.[c.eng_name]);
      try {
        await addOrUpdateFromCell({
          table,
          key,
          field,
          name,
          unit,
          kind: isBoolType(col?.data_type) ? "switch" : "analog",
          axis
        });
        el("st-add-dialog")?.close();
      } catch (error) {
        errEl.textContent = error.message || String(error);
      }
    });

    // 曲线板工具栏：区间模式、窗口时长、对齐步长、起止时间
    el("st-range-mode")?.addEventListener("change", (e) => {
      const next = clone(state.settings);
      next.view.range_mode = e.target.value;
      if (next.view.range_mode === "fixed" && (!next.view.from || !next.view.until)) {
        const nowMs = Date.now() + state.serverOffsetMs;
        const winMs = (next.view.window_minutes || 15) * 60 * 1000;
        next.view.from = formatDateTimeInput(nowMs - winMs);
        next.view.until = formatDateTimeInput(nowMs);
      }
      void saveSettings(next, { silent: true });
    });

    el("st-window-minutes")?.addEventListener("change", (e) => {
      const next = clone(state.settings);
      next.view.window_minutes = Math.max(1, Math.min(1440, Number(e.target.value) || 15));
      void saveSettings(next, { silent: true });
    });

    el("st-step-seconds")?.addEventListener("change", (e) => {
      const next = clone(state.settings);
      next.view.step_seconds = Number(e.target.value) || 0;
      void saveSettings(next, { silent: true });
    });

    const commitFixedRange = () => {
      const fromVal = el("st-range-from")?.value || "";
      const untilVal = el("st-range-until")?.value || "";
      const fromMs = parseDateTimeInput(fromVal);
      const untilMs = parseDateTimeInput(untilVal);
      if (fromMs !== null && untilMs !== null && untilMs > fromMs) {
        const next = clone(state.settings);
        next.view.range_mode = "fixed";
        next.view.from = fromVal;
        next.view.until = untilVal;
        void saveSettings(next, { silent: true });
      }
    };
    el("st-range-from")?.addEventListener("change", commitFixedRange);
    el("st-range-until")?.addEventListener("change", commitFixedRange);

    el("st-zoom-in")?.addEventListener("click", () => zoomWindow(0.5));
    el("st-zoom-out")?.addEventListener("click", () => zoomWindow(2.0));
    el("st-zoom-reset")?.addEventListener("click", resetView);
    el("st-axes-btn")?.addEventListener("click", openAxesDialog);
    el("st-series-btn")?.addEventListener("click", openSeriesDialog);
    el("st-export-csv")?.addEventListener("click", exportCsv);

    // 纵轴设置弹窗
    for (const side of ["left", "right"]) {
      el(`st-axis-${side}-auto`)?.addEventListener("change", (e) => {
        const auto = e.target.checked;
        if (el(`st-axis-${side}-min`)) el(`st-axis-${side}-min`).disabled = auto;
        if (el(`st-axis-${side}-max`)) el(`st-axis-${side}-max`).disabled = auto;
      });
    }
    el("st-axes-cancel")?.addEventListener("click", () => el("st-axes-dialog")?.close());
    el("st-axes-close-x")?.addEventListener("click", () => el("st-axes-dialog")?.close());
    el("st-axes-apply")?.addEventListener("click", async () => {
      const errEl = el("st-axes-error");
      const next = clone(state.settings);
      for (const side of ["left", "right"]) {
        const auto = Boolean(el(`st-axis-${side}-auto`)?.checked);
        const minRaw = el(`st-axis-${side}-min`)?.value;
        const maxRaw = el(`st-axis-${side}-max`)?.value;
        const unit = (el(`st-axis-${side}-unit`)?.value || "").trim();
        const minVal = minRaw === "" ? null : Number(minRaw);
        const maxVal = maxRaw === "" ? null : Number(maxRaw);
        if (!auto && minVal !== null && maxVal !== null && minVal >= maxVal) {
          errEl.textContent = tr("st.axisRangeErr", null, "纵轴最小值必须小于最大值");
          return;
        }
        next.view.axes[side] = { auto, min: minVal, max: maxVal, unit };
      }
      try {
        await saveSettings(next);
        el("st-axes-dialog")?.close();
      } catch (error) {
        errEl.textContent = error.message || String(error);
      }
    });

    // 曲线设置弹窗
    el("st-series-cancel")?.addEventListener("click", () => el("st-series-dialog")?.close());
    el("st-series-close-x")?.addEventListener("click", () => el("st-series-dialog")?.close());
    el("st-series-apply")?.addEventListener("click", async () => {
      const errEl = el("st-series-error");
      const next = clone(state.settings);
      const rows = el("st-series-dialog-body")?.querySelectorAll(".st-series-edit-row") || [];
      for (const row of rows) {
        const key = row.dataset.seriesKey;
        const item = next.series.find((s) => s.key === key);
        if (!item) continue;
        item.visible = Boolean(row.querySelector(".st-ed-vis")?.checked);
        item.color = row.querySelector(".st-ed-color")?.value || item.color;
        const nameVal = (row.querySelector(".st-ed-name")?.value || "").trim();
        if (nameVal) item.name = nameVal;
        item.unit = (row.querySelector(".st-ed-unit")?.value || "").trim();
        item.axis = row.querySelector(".st-ed-axis")?.value || "left";
        item.width = Number(row.querySelector(".st-ed-width")?.value || 2);
      }
      try {
        await saveSettings(next);
        el("st-series-dialog")?.close();
      } catch (error) {
        errEl.textContent = error.message || String(error);
      }
    });

    // Canvas 交互：悬停十字准星、点击图例、滚轮缩放、右键菜单
    const canvas = el("st-canvas");
    if (canvas) {
      canvas.addEventListener("mousemove", (event) => {
        const rect = canvas.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        const hit = hitTestLegend(canvas, event.clientX, event.clientY);
        canvas.style.cursor = hit ? "pointer" : "crosshair";
        state.hover = hit ? null : { x, y };
        drawChart();
      });
      canvas.addEventListener("mouseleave", () => {
        state.hover = null;
        drawChart();
      });
      canvas.addEventListener("click", (event) => {
        const hit = hitTestLegend(canvas, event.clientX, event.clientY);
        if (!hit) return;
        if (hit.overflow) {
          openSeriesDialog();
          return;
        }
        const next = clone(state.settings);
        const item = next.series.find((s) => s.key === hit.key);
        if (!item) return;
        item.visible = item.visible === false;
        void saveSettings(next, { silent: true });
      });
      canvas.addEventListener("wheel", (event) => {
        if (!(state.settings.series || []).length) return;
        event.preventDefault();
        zoomWindow(event.deltaY < 0 ? 0.75 : 1.35);
      }, { passive: false });
      canvas.addEventListener("contextmenu", (event) => {
        const hit = hitTestLegend(canvas, event.clientX, event.clientY);
        openChartMenu(event, hit && !hit.overflow ? hit.key : null);
      });
    }

    // 曲线区右键菜单点击
    el("st-chart-context-menu")?.addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-st-action]");
      if (!btn) return;
      const action = btn.dataset.stAction;
      const hitKey = state.contextSeriesKey;
      closeChartMenu();
      if (action === "switch-axis" && hitKey) {
        const next = clone(state.settings);
        const item = next.series.find((s) => s.key === hitKey);
        if (item) {
          item.axis = item.axis === "right" ? "left" : "right";
          void saveSettings(next, { silent: true });
        }
      } else if (action === "toggle-visible" && hitKey) {
        const next = clone(state.settings);
        const item = next.series.find((s) => s.key === hitKey);
        if (item) {
          item.visible = item.visible === false;
          void saveSettings(next, { silent: true });
        }
      } else if (action === "remove-series" && hitKey) {
        void confirmAndRemoveSeries(hitKey);
      } else if (action === "axes-settings") {
        openAxesDialog();
      } else if (action === "series-settings") {
        openSeriesDialog();
      } else if (action === "zoom-in") {
        zoomWindow(0.5);
      } else if (action === "zoom-out") {
        zoomWindow(2.0);
      } else if (action === "zoom-reset") {
        resetView();
      } else if (action === "export-csv") {
        exportCsv();
      }
    });

    document.addEventListener("click", (e) => {
      if (!e.target.closest("#st-chart-context-menu")) closeChartMenu();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeChartMenu();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopPolling();
      else if (state.active && state.database) void syncFromServer({ incremental: true });
    });

    const wrap = el("st-chart-wrap");
    if (wrap && typeof ResizeObserver === "function") {
      new ResizeObserver(() => {
        if (state.active) requestAnimationFrame(drawChart);
      }).observe(wrap);
    }
  }

  /* ==================== 对外公开接口 ==================== */
  return {
    isSampleableType,
    isBoolType,
    pointKey,
    init(hostApi) {
      state.host = hostApi;
      if (state.initialized) return;
      state.initialized = true;
      loadLayout();
      bindEvents();
      applyLayout();
      renderAll();
    },
    setContext(workspace, database) {
      const ws = workspace || "";
      const db = database || "";
      if (ws === state.workspace && db === state.database) return;
      stopPolling();
      state.workspace = ws;
      state.database = db;
      state.historyLoaded = false;
      state.revision = null;
      state.samples = [];
      state.lastSampleMs = 0;
      state.focusedKey = null;
      state.error = "";
      state.settings = defaultSettings();
      rebuildSeriesMap();
      notifyHostSeriesChanged();
      renderAll();
      if (db) {
        void syncFromServer({ configOnly: !state.active, incremental: false });
      }
    },
    setActive(active) {
      const next = Boolean(active);
      if (next === state.active) return;
      state.active = next;
      if (next) {
        applyLayout();
        if (state.database) {
          void syncFromServer({ configOnly: false, incremental: state.historyLoaded });
        } else {
          renderAll();
        }
      } else {
        stopPolling();
        closeChartMenu();
      }
    },
    isSamplePoint(table, key, field) {
      if (!table || !field || !key) return null;
      return state.seriesMap.get(pointKey(table, key, field)) || null;
    },
    addOrUpdateFromCell,
    toggleAxisForCell,
    removeFromCell,
    focusPoint,
    refresh() {
      if (!state.database) return;
      void syncFromServer({ configOnly: !state.active, incremental: state.historyLoaded });
    },
    redraw() {
      renderAll();
    },
    getSeriesCount() {
      return (state.settings.series || []).length;
    }
  };
})();
