const state = { table: null, tables: [], rows: [], columns: [], selectSeq: 0, querySeq: 0 };
const $ = (id) => document.getElementById(id);
const numericKinds = new Set(["int", "integer", "long", "float", "double", "number", "decimal"]);
const themeKey = "rtdb.ui.theme";
const uiThemeAliases = { light: "minimalist-light", dark: "precision-dark" };
const uiThemes = new Set(["minimalist-light", "precision-dark", "bento-grid", "glassmorphism", "editorial", "neo-brutalism", "sci-fi"]);
let noticeTimer = null;

/** 切换并记忆多视觉风格（兼容历史 light/dark 存储值） */
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
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

/** 根据当前表字段刷新排序下拉选择器 */
function syncOrderControls(columns) {
  const fieldEl = $("order-field"), dirEl = $("order-dir");
  if (!fieldEl || !dirEl) return;
  const previous = fieldEl.value;
  fieldEl.replaceChildren(new Option("默认排序", ""));
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

async function loadTables(selectName = state.table) {
  const payload = await request("/api/schemas");
  state.tables = payload.schemas || [];
  const list = $("table-list");
  list.replaceChildren();
  if (!state.tables.length) {
    list.innerHTML = '<p class="muted empty">暂无数据表，点击“新建”开始。</p>';
    return;
  }
  for (const item of state.tables) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `table-item${item.name === selectName ? " active" : ""}`;
    const nameSpan = document.createElement("span");
    nameSpan.textContent = item.name;
    const countSmall = document.createElement("small");
    countSmall.textContent = `${item.row_count} 行`;
    button.append(nameSpan, countSmall);
    button.onclick = () => selectTable(item.name);
    list.append(button);
  }
  if (selectName && state.tables.some((item) => item.name === selectName)) await selectTable(selectName);
  else await selectTable(state.tables[0].name);
}

async function selectTable(name) {
  const seq = ++state.selectSeq;
  state.table = name;
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

async function queryRows() {
  if (!state.table) return;
  const tableName = state.table, seq = ++state.querySeq;
  updateOrderByHidden();
  const query = new URLSearchParams();
  if ($("where").value.trim()) query.set("where", $("where").value.trim());
  if ($("order-by").value.trim()) query.set("order_by", $("order-by").value.trim());
  const payload = await request(`/api/data/${encodeURIComponent(tableName)}?${query}`);
  if (seq !== state.querySeq || state.table !== tableName) return;
  state.rows = payload.rows || [];
  if ($("table-summary")) $("table-summary").textContent = `${tableName} · 共 ${state.rows.length.toLocaleString()} 条`;
  renderGrid();
}

function isNumericColumn(name) {
  const col = state.columns.find((item) => item.eng_name === name);
  if (col) return numericKinds.has(String(col.data_type || "").toLowerCase());
  return state.rows.some((row) => typeof row[name] === "number");
}

function renderGrid() {
  const target = $("data-grid");
  target.replaceChildren();
  if (!state.rows.length) {
    target.innerHTML = '<p class="muted empty">暂无数据。</p>';
    return;
  }
  const columns = [...new Set(state.rows.flatMap((row) => Object.keys(row)))];
  const numericMap = new Map(columns.map((col) => [col, isNumericColumn(col)]));
  const table = document.createElement("table");
  const head = document.createElement("tr");
  head.innerHTML = `<th>操作</th>${columns.map((column) => `<th class="${numericMap.get(column) ? "is-numeric" : ""}">${escapeHtml(column)}</th>`).join("")}`;
  const thead = document.createElement("thead");
  thead.append(head);
  table.append(thead);
  const body = document.createElement("tbody");
  state.rows.forEach((row, index) => {
    const tr = document.createElement("tr");
    const action = document.createElement("td");
    action.innerHTML = `<div class="row-actions"><button type="button" class="small secondary">编辑</button><input type="checkbox" data-index="${index}" aria-label="选择第 ${index + 1} 行"></div>`;
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
  target.append(table);
}

function escapeHtml(value) { return value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char])); }
function openRowDialog(index = null) {
  $("row-dialog-title").textContent = index == null ? "新增行" : "编辑行";
  $("row-editor").value = JSON.stringify(index == null ? {} : state.rows[index], null, 2);
  $("row-dialog").dataset.index = index == null ? "" : String(index);
  $("row-dialog").showModal();
}
function editRow(index) { openRowDialog(index); }

if ($("theme-select")) $("theme-select").onchange = () => applyTheme($("theme-select").value);
$("order-field").onchange = () => { updateOrderByHidden(); queryRows().catch((error) => notice(error.message, true)); };
$("order-dir").onchange = () => { updateOrderByHidden(); queryRows().catch((error) => notice(error.message, true)); };
$("where").onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); queryRows().catch((error) => notice(error.message, true)); } };
$("refresh").onclick = () => loadTables(state.table).catch((error) => notice(error.message, true));
$("new-table").onclick = () => {
  state.table = null;
  state.columns = [];
  syncOrderControls([]);
  if ($("table-summary")) $("table-summary").textContent = "";
  $("schema-editor").value = JSON.stringify({ head: { chn_name: "", eng_name: "" }, columns: [] }, null, 2);
  $("data-grid").innerHTML = '<p class="muted empty">保存结构后即可录入数据。</p>';
};
$("save-schema").onclick = async () => {
  try {
    const payload = JSON.parse($("schema-editor").value);
    const name = payload.head?.eng_name;
    if (!name) throw new Error("head.eng_name 不能为空");
    const result = await request(`/api/schemas/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(payload) });
    state.table = name;
    notice(`表结构已保存：${result.name}`);
    await loadTables(name);
  } catch (error) { notice(error.message, true); }
};
$("delete-schema").onclick = async () => {
  if (!state.table || !await confirmModal("删除表结构", `确认删除表 ${state.table} 的定义与数据？此操作不可撤销。`)) return;
  try {
    await request(`/api/schemas/${encodeURIComponent(state.table)}`, { method: "DELETE" });
    notice("表结构已删除");
    state.table = null;
    await loadTables();
  } catch (error) { notice(error.message, true); }
};
$("query").onclick = () => queryRows().catch((error) => notice(error.message, true));
$("add-row").onclick = () => { if (state.table) openRowDialog(); else notice("请先选择或保存一张表", true); };
$("save-row").onclick = async (event) => {
  event.preventDefault();
  try {
    const row = JSON.parse($("row-editor").value);
    const index = $("row-dialog").dataset.index;
    if (index === "") await request(`/api/data/${encodeURIComponent(state.table)}`, { method: "POST", body: JSON.stringify(row) });
    else await request(`/api/data/${encodeURIComponent(state.table)}/${index}`, { method: "PUT", body: JSON.stringify(row) });
    $("row-dialog").close();
    await queryRows();
    await loadTables(state.table);
    notice("数据已保存");
  } catch (error) { notice(error.message, true); }
};
$("delete-rows").onclick = async () => {
  const indexes = [...document.querySelectorAll("#data-grid input[type=checkbox]:checked")].map((input) => Number(input.dataset.index));
  if (!indexes.length) return notice("请选择要删除的行", true);
  if (!await confirmModal("删除选中记录", `确认删除表 ${state.table} 中选中的 ${indexes.length} 条记录？此操作不可撤销。`)) return;
  try {
    for (const index of indexes.reverse()) await request(`/api/data/${encodeURIComponent(state.table)}/${index}`, { method: "DELETE" });
    await queryRows();
    await loadTables(state.table);
    notice(`已删除 ${indexes.length} 条记录`);
  } catch (error) { notice(error.message, true); }
};
loadTables().catch((error) => notice(error.message, true));
