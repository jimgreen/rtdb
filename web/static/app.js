const state = { table: null, tables: [], rows: [], columns: [], selectSeq: 0, querySeq: 0, creatingNew: false, filteredOrOrdered: false };
const $ = (id) => document.getElementById(id);
const i18n = window.rtdbI18n || { t: (k) => k, setLang: () => {}, onChange: () => {} };
const t = (key, params) => i18n.t(key, params);
const numericKinds = new Set(["int", "integer", "long", "float", "double", "number", "decimal"]);
const themeKey = "rtdb.ui.theme";
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
let noticeTimer = null;

/** 切换并记忆多视觉风格（兼容历史存储别名） */
function normalizeTheme(value) {
  const mapped = uiThemeAliases[value] || value;
  return uiThemes.has(mapped) ? mapped : "minimalist-light";
}
function applyTheme(mode, persist = true) {
  const theme = normalizeTheme(mode);
  document.documentElement.dataset.theme = theme;
  const select = $("theme-select");
  if (select && select.value !== theme) select.value = theme;
  if (persist) { try { localStorage.setItem(themeKey, theme); } catch { /* 忽略存储异常 */ } }
}
try { applyTheme(localStorage.getItem(themeKey) || "minimalist-light", false); } catch { applyTheme("minimalist-light", false); }

function notice(message, error = false) {
  clearTimeout(noticeTimer);
  const box = $("notice");
  box.hidden = !message;
  box.textContent = message;
  box.className = `notice${error ? " error" : ""}`;
  if (message) noticeTimer = setTimeout(() => { box.hidden = true; }, 4500);
}

/** 统一高危确认弹窗，默认聚焦取消按钮防止误触 */
function confirmModal(title, message) {
  const dialog = $("confirm-dialog");
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = message;
  dialog.returnValue = "";
  dialog.showModal();
  $("confirm-cancel")?.focus({ preventScroll: true });
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirmed"), { once: true }));
}

async function request(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || t("notice.requestFailed", { status: response.status }));
  return payload;
}

/** 根据当前表字段刷新排序下拉选择器 */
function syncOrderControls(columns) {
  const fieldEl = $("order-field"), dirEl = $("order-dir");
  if (!fieldEl || !dirEl) return;
  const previous = fieldEl.value;
  fieldEl.replaceChildren(new Option(t("filter.defaultOrder"), ""));
  for (const col of columns) {
    const label = col.chn_name && col.chn_name !== col.eng_name ? `${col.chn_name} / ${col.eng_name}` : col.eng_name;
    fieldEl.append(new Option(label, col.eng_name));
  }
  fieldEl.value = columns.some((c) => c.eng_name === previous) ? previous : "";
  dirEl.disabled = !fieldEl.value;
  updateOrderByHidden();
}

function updateOrderByHidden() {
  const field = $("order-field")?.value || "";
  const dir = $("order-dir")?.value || "ASC";
  if ($("order-dir")) $("order-dir").disabled = !field;
  if ($("order-by")) $("order-by").value = field ? `${field} ${dir}` : "";
}

function renderTableList() {
  const list = $("table-list");
  list.replaceChildren();
  const keyword = ($("table-search")?.value || "").trim().toLowerCase();
  const visible = keyword ? state.tables.filter((item) => item.name.toLowerCase().includes(keyword)) : state.tables;
  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "muted empty";
    empty.textContent = t(state.tables.length ? "grid.emptyRows" : "grid.noTables");
    list.append(empty);
    return;
  }
  for (const item of visible) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `table-item${item.name === state.table ? " active" : ""}`;
    button.title = item.name;
    const nameSpan = document.createElement("span");
    nameSpan.textContent = item.name;
    const countSmall = document.createElement("small");
    countSmall.textContent = t("grid.rowCount", { count: item.row_count });
    button.append(nameSpan, countSmall);
    button.onclick = () => selectTable(item.name);
    list.append(button);
  }
}

async function refreshTableCounts() {
  const payload = await request("/api/schemas");
  state.tables = payload.schemas || [];
  renderTableList();
}

async function loadTables(selectName = state.table) {
  const payload = await request("/api/schemas");
  state.tables = payload.schemas || [];
  if (selectName && state.tables.some((item) => item.name === selectName)) {
    state.table = selectName;
    renderTableList();
    await selectTable(selectName);
  } else if (state.tables.length) {
    state.table = state.tables[0].name;
    renderTableList();
    await selectTable(state.tables[0].name);
  } else {
    state.table = null;
    renderTableList();
  }
}

async function selectTable(name) {
  const seq = ++state.selectSeq;
  state.table = name;
  state.creatingNew = false;
  await loadTablesOnlyActive();
  const item = await request(`/api/schemas/${encodeURIComponent(name)}`);
  if (seq !== state.selectSeq) return;
  const def = item.definition || { head: { chn_name: "", eng_name: name }, columns: [] };
  state.columns = Array.isArray(def.columns) ? def.columns : [];
  syncOrderControls(state.columns);
  $("schema-editor").value = JSON.stringify(def, null, 2);
  await queryRows();
}

async function loadTablesOnlyActive() {
  const list = $("table-list");
  for (const button of list.querySelectorAll("button")) {
    button.classList.toggle("active", button.querySelector("span")?.textContent === state.table);
  }
}

function updateSummary() {
  if (!$("table-summary")) return;
  $("table-summary").textContent = state.table
    ? t("grid.summary", { table: state.table, count: state.rows.length.toLocaleString() })
    : "";
}

async function queryRows() {
  if (!state.table) return;
  const tableName = state.table, seq = ++state.querySeq;
  updateOrderByHidden();
  const query = new URLSearchParams();
  const whereVal = $("where").value.trim();
  const orderVal = $("order-by").value.trim();
  if (whereVal) query.set("where", whereVal);
  if (orderVal) query.set("order_by", orderVal);
  const payload = await request(`/api/data/${encodeURIComponent(tableName)}?${query}`);
  if (seq !== state.querySeq || state.table !== tableName) return;
  state.rows = payload.rows || [];
  state.filteredOrOrdered = Boolean(whereVal || orderVal);
  updateSummary();
  renderGrid();
}

function isNumericColumn(name) {
  const col = state.columns.find((item) => item.eng_name === name);
  if (col) return numericKinds.has(String(col.data_type || "").toLowerCase());
  return state.rows.some((row) => typeof row[name] === "number");
}

function renderGrid(preserveChecked = null) {
  const target = $("data-grid");
  target.replaceChildren();
  if (!state.table) {
    const empty = document.createElement("p");
    empty.className = "muted empty";
    empty.textContent = t(state.creatingNew ? "grid.saveSchemaFirst" : "grid.selectTable");
    target.append(empty);
    return;
  }
  if (!state.rows.length) {
    const empty = document.createElement("p");
    empty.className = "muted empty";
    empty.textContent = t("grid.emptyRows");
    target.append(empty);
    return;
  }
  const columns = [...new Set(state.rows.flatMap((row) => Object.keys(row)))];
  const numericMap = new Map(columns.map((col) => [col, isNumericColumn(col)]));
  const table = document.createElement("table");
  const head = document.createElement("tr");
  head.innerHTML = `<th><div class="row-actions"><input id="grid-select-all" type="checkbox" aria-label="${escapeHtml(t("grid.selectAllAria"))}"><span>${escapeHtml(t("grid.colAction"))}</span></div></th>${columns.map((column) => `<th class="${numericMap.get(column) ? "is-numeric" : ""}">${escapeHtml(column)}</th>`).join("")}`;
  const thead = document.createElement("thead");
  thead.append(head);
  table.append(thead);
  const body = document.createElement("tbody");
  const rowBoxes = [];
  const syncSelectAll = () => {
    const selectAll = head.querySelector("#grid-select-all");
    if (!selectAll) return;
    const checkedCount = rowBoxes.filter((cb) => cb.checked).length;
    selectAll.checked = rowBoxes.length > 0 && checkedCount === rowBoxes.length;
    selectAll.indeterminate = checkedCount > 0 && checkedCount < rowBoxes.length;
  };
  state.rows.forEach((row, index) => {
    const tr = document.createElement("tr");
    const action = document.createElement("td");
    action.innerHTML = `<div class="row-actions"><button type="button" class="small secondary">${escapeHtml(t("btn.edit"))}</button><input type="checkbox" data-index="${index}" aria-label="${escapeHtml(`${t("btn.edit")} #${index + 1}`)}"></div>`;
    const checkbox = action.querySelector("input[type=checkbox]");
    rowBoxes.push(checkbox);
    if (preserveChecked?.has(index)) {
      checkbox.checked = true;
      tr.classList.add("row-selected");
    }
    checkbox.onchange = () => {
      tr.classList.toggle("row-selected", checkbox.checked);
      syncSelectAll();
    };
    action.querySelector("button").onclick = () => editRow(index);
    tr.append(action);
    for (const column of columns) {
      const td = document.createElement("td");
      const val = row[column];
      td.className = `value${numericMap.get(column) ? " is-numeric" : ""}`;
      td.title = val == null ? "NULL" : typeof val === "object" ? JSON.stringify(val) : String(val);
      if (val == null) {
        const span = document.createElement("span");
        span.className = "cell-null";
        span.textContent = "NULL";
        td.append(span);
      } else {
        td.textContent = typeof val === "object" ? JSON.stringify(val) : String(val);
      }
      tr.append(td);
    }
    body.append(tr);
  });
  table.append(body);
  const selectAll = head.querySelector("#grid-select-all");
  if (selectAll) {
    selectAll.onchange = () => {
      for (const cb of rowBoxes) {
        cb.checked = selectAll.checked;
        cb.closest("tr")?.classList.toggle("row-selected", cb.checked);
      }
      selectAll.indeterminate = false;
    };
    syncSelectAll();
  }
  target.append(table);
}

function escapeHtml(value) { return value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char])); }
function defaultRowSkeleton() {
  if (!state.columns.length) return {};
  const skeleton = {};
  for (const col of state.columns) {
    if (col.default_value !== undefined) {
      skeleton[col.eng_name] = col.default_value;
    } else {
      const kind = String(col.data_type || "").toLowerCase();
      if (["int", "integer", "long"].includes(kind)) skeleton[col.eng_name] = 0;
      else if (["float", "double", "number", "decimal"].includes(kind)) skeleton[col.eng_name] = 0.0;
      else if (["bool", "boolean"].includes(kind)) skeleton[col.eng_name] = false;
      else if (["list", "array"].includes(kind)) skeleton[col.eng_name] = [];
      else if (["dict", "object", "json"].includes(kind)) skeleton[col.eng_name] = {};
      else skeleton[col.eng_name] = "";
    }
  }
  return skeleton;
}
function openRowDialog(index = null) {
  $("row-dialog-title").textContent = index == null ? t("dialog.addRowTitle") : t("dialog.editRowTitle");
  if ($("row-error")) $("row-error").textContent = "";
  $("row-editor").value = JSON.stringify(index == null ? defaultRowSkeleton() : state.rows[index], null, 2);
  $("row-dialog").dataset.index = index == null ? "" : String(index);
  $("row-dialog").showModal();
  $("row-editor").focus({ preventScroll: true });
}
function editRow(index) { openRowDialog(index); }

if ($("lang-select")) $("lang-select").onchange = () => i18n.setLang($("lang-select").value);
if ($("theme-select")) $("theme-select").onchange = () => applyTheme($("theme-select").value);
/** 构造行精准定位条件（筛选/排序下按主键或原始整行匹配，避免按视图下标误删误改） */
function buildRowMatchWhere(row) {
  const pkCols = state.columns.filter((c) => c.primary_key).map((c) => c.eng_name);
  if (pkCols.length && pkCols.every((k) => row && Object.prototype.hasOwnProperty.call(row, k))) {
    return Object.fromEntries(pkCols.map((k) => [k, row[k]]));
  }
  return { ...(row || {}) };
}

i18n.onChange(() => {
  const checked = new Set(
    [...document.querySelectorAll("#data-grid input[type=checkbox][data-index]:checked")].map((el) => Number(el.dataset.index))
  );
  renderTableList();
  syncOrderControls(state.columns);
  updateSummary();
  renderGrid(checked);
});

$("order-field").onchange = () => { updateOrderByHidden(); queryRows().catch((error) => notice(error.message, true)); };
$("order-dir").onchange = () => { updateOrderByHidden(); queryRows().catch((error) => notice(error.message, true)); };
$("where").onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); queryRows().catch((error) => notice(error.message, true)); } };
$("refresh").onclick = () => loadTables(state.table).catch((error) => notice(error.message, true));
$("new-table").onclick = () => {
  state.table = null;
  state.creatingNew = true;
  state.columns = [];
  syncOrderControls([]);
  updateSummary();
  $("schema-editor").value = JSON.stringify({ head: { chn_name: "", eng_name: "" }, columns: [] }, null, 2);
  renderGrid();
};
$("save-schema").onclick = async () => {
  try {
    const payload = JSON.parse($("schema-editor").value);
    const name = payload.head?.eng_name;
    if (!name) throw new Error(t("notice.engNameRequired"));
    const result = await request(`/api/schemas/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(payload) });
    state.table = name;
    notice(t("notice.schemaSaved", { name: result.name }));
    await loadTables(name);
  } catch (error) { notice(error.message, true); }
};
$("delete-schema").onclick = async () => {
  if (!state.table || !await confirmModal(t("dialog.deleteSchemaTitle"), t("dialog.deleteSchemaMsg", { table: state.table }))) return;
  try {
    await request(`/api/schemas/${encodeURIComponent(state.table)}`, { method: "DELETE" });
    notice(t("notice.schemaDeleted"));
    state.table = null;
    await loadTables();
  } catch (error) { notice(error.message, true); }
};
$("query").onclick = () => queryRows().catch((error) => notice(error.message, true));
$("add-row").onclick = () => { if (state.table) openRowDialog(); else notice(t("notice.selectTableFirst"), true); };
if ($("table-search")) {
  $("table-search").oninput = () => renderTableList();
  $("table-search").onkeydown = (event) => {
    if (event.key === "Escape" && $("table-search").value) {
      event.preventDefault();
      $("table-search").value = "";
      renderTableList();
    } else if (event.key === "Enter") {
      const first = $("table-list")?.querySelector("button.table-item");
      if (first) { event.preventDefault(); first.click(); }
    }
  };
}
if ($("row-editor")) {
  $("row-editor").onkeydown = (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      $("save-row")?.click();
    }
  };
}
$("save-row").onclick = async (event) => {
  event.preventDefault();
  if ($("row-error")) $("row-error").textContent = "";
  try {
    const row = JSON.parse($("row-editor").value);
    const index = $("row-dialog").dataset.index;
    if (index === "") {
      await request(`/api/data/${encodeURIComponent(state.table)}`, { method: "POST", body: JSON.stringify(row) });
    } else if (state.filteredOrOrdered) {
      const original = state.rows[Number(index)];
      await request(`/api/data/${encodeURIComponent(state.table)}`, {
        method: "PUT",
        body: JSON.stringify({ values: row, where: buildRowMatchWhere(original), limit: 1 })
      });
    } else {
      await request(`/api/data/${encodeURIComponent(state.table)}/${index}`, { method: "PUT", body: JSON.stringify(row) });
    }
    $("row-dialog").close();
    await queryRows();
    await refreshTableCounts();
    notice(t("notice.rowSaved"));
  } catch (error) {
    if ($("row-error")) $("row-error").textContent = error.message;
    else notice(error.message, true);
  }
};
$("delete-rows").onclick = async () => {
  const indexes = [...document.querySelectorAll("#data-grid input[type=checkbox][data-index]:checked")].map((input) => Number(input.dataset.index));
  if (!indexes.length) return notice(t("notice.selectRowsFirst"), true);
  if (!await confirmModal(t("dialog.deleteRowsTitle"), t("dialog.deleteRowsMsg", { table: state.table, count: indexes.length }))) return;
  try {
    if (state.filteredOrOrdered) {
      for (const index of indexes) {
        const targetRow = state.rows[index];
        await request(`/api/data/${encodeURIComponent(state.table)}`, {
          method: "DELETE",
          body: JSON.stringify({ where: buildRowMatchWhere(targetRow), limit: 1 })
        });
      }
    } else {
      for (const index of indexes.reverse()) await request(`/api/data/${encodeURIComponent(state.table)}/${index}`, { method: "DELETE" });
    }
    await queryRows();
    await refreshTableCounts();
    notice(t("notice.rowsDeleted", { count: indexes.length }));
  } catch (error) { notice(error.message, true); }
};
loadTables().catch((error) => notice(error.message, true));
