const state = { table: null, tables: [], rows: [] };
const $ = (id) => document.getElementById(id);

function notice(message, error = false) {
  const box = $("notice"); box.hidden = !message; box.textContent = message; box.className = `notice${error ? " error" : ""}`;
  if (message) setTimeout(() => { box.hidden = true; }, 4500);
}

async function request(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

async function loadTables(selectName = state.table) {
  const payload = await request("/api/tables"); state.tables = payload.tables || [];
  const list = $("table-list"); list.replaceChildren();
  if (!state.tables.length) { list.innerHTML = '<p class="muted">还没有表，点击“新建”。</p>'; return; }
  for (const item of state.tables) {
    const button = document.createElement("button"); button.className = `table-item${item.name === selectName ? " active" : ""}`;
    button.innerHTML = `<span>${item.name}</span><small>${item.row_count} 行</small>`; button.onclick = () => selectTable(item.name); list.append(button);
  }
  if (selectName && state.tables.some((item) => item.name === selectName)) await selectTable(selectName);
  else await selectTable(state.tables[0].name);
}

async function selectTable(name) {
  state.table = name; const item = await request(`/api/tables/${encodeURIComponent(name)}`);
  $("schema-editor").value = JSON.stringify(item.definition || { head: { chn_name: "", eng_name: name, max_elements: 1000 }, columns: [] }, null, 2);
  await queryRows(); await loadTablesOnlyActive();
}

async function loadTablesOnlyActive() {
  const list = $("table-list"); for (const button of list.querySelectorAll("button")) button.classList.toggle("active", button.querySelector("span")?.textContent === state.table);
}

async function queryRows() {
  if (!state.table) return; const query = new URLSearchParams(); if ($("where").value.trim()) query.set("where", $("where").value.trim()); if ($("order-by").value.trim()) query.set("order_by", $("order-by").value.trim());
  const payload = await request(`/api/tables/${encodeURIComponent(state.table)}/data?${query}`); state.rows = payload.rows || []; renderGrid();
}

function renderGrid() {
  const target = $("data-grid"); target.replaceChildren(); if (!state.rows.length) { target.innerHTML = '<p class="muted" style="padding:12px">暂无数据。</p>'; return; }
  const columns = [...new Set(state.rows.flatMap((row) => Object.keys(row)))]; const table = document.createElement("table"); const head = document.createElement("tr");
  head.innerHTML = `<th>操作</th>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}`; const thead = document.createElement("thead"); thead.append(head); table.append(thead);
  const body = document.createElement("tbody"); state.rows.forEach((row, index) => { const tr = document.createElement("tr"); const action = document.createElement("td"); action.innerHTML = `<button class="small secondary">编辑</button> <input type="checkbox" data-index="${index}">`; action.querySelector("button").onclick = () => editRow(index); tr.append(action); for (const column of columns) { const td = document.createElement("td"); td.className = "value"; td.title = JSON.stringify(row[column]); td.textContent = row[column] == null ? "" : typeof row[column] === "object" ? JSON.stringify(row[column]) : String(row[column]); tr.append(td); } body.append(tr); }); table.append(body); target.append(table);
}

function escapeHtml(value) { return value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char])); }
function openRowDialog(index = null) { $("row-dialog-title").textContent = index == null ? "新增行" : "编辑行"; $("row-editor").value = JSON.stringify(index == null ? {} : state.rows[index], null, 2); $("row-dialog").dataset.index = index == null ? "" : String(index); $("row-dialog").showModal(); }
function editRow(index) { openRowDialog(index); }

$("refresh").onclick = () => loadTables(state.table).catch((error) => notice(error.message, true));
$("new-table").onclick = () => { state.table = null; $("schema-editor").value = JSON.stringify({ head: { chn_name: "", eng_name: "", max_elements: 1000 }, columns: [] }, null, 2); $("data-grid").innerHTML = '<p class="muted" style="padding:12px">保存结构后即可录入数据。</p>'; };
$("save-schema").onclick = async () => { try { const payload = JSON.parse($("schema-editor").value); const name = payload.head?.eng_name; if (!name) throw new Error("head.eng_name 不能为空"); const result = await request(`/api/tables/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(payload) }); state.table = name; notice(`表结构已保存：${result.name}`); await loadTables(name); } catch (error) { notice(error.message, true); } };
$("delete-schema").onclick = async () => { if (!state.table || !confirm(`删除表 ${state.table} 的定义？`)) return; try { await request(`/api/tables/${encodeURIComponent(state.table)}`, { method: "DELETE" }); notice("表结构已删除"); state.table = null; await loadTables(); } catch (error) { notice(error.message, true); } };
$("query").onclick = () => queryRows().catch((error) => notice(error.message, true));
$("add-row").onclick = () => { if (state.table) openRowDialog(); else notice("请先选择或保存一张表", true); };
$("save-row").onclick = async (event) => { event.preventDefault(); try { const row = JSON.parse($("row-editor").value); const index = $("row-dialog").dataset.index; if (index === "") await request(`/api/tables/${encodeURIComponent(state.table)}/data`, { method: "POST", body: JSON.stringify(row) }); else await request(`/api/data/${encodeURIComponent(state.table)}/${index}`, { method: "PUT", body: JSON.stringify(row) }); $("row-dialog").close(); await queryRows(); await loadTables(state.table); notice("数据已保存"); } catch (error) { notice(error.message, true); } };
$("delete-rows").onclick = async () => { const indexes = [...document.querySelectorAll("#data-grid input[type=checkbox]:checked")].map((input) => Number(input.dataset.index)); if (!indexes.length) return notice("请选择要删除的行", true); if (!confirm(`删除 ${indexes.length} 行？`)) return; try { for (const index of indexes.reverse()) await request(`/api/data/${encodeURIComponent(state.table)}/${index}`, { method: "DELETE" }); await queryRows(); await loadTables(state.table); notice("数据已删除"); } catch (error) { notice(error.message, true); } };
loadTables().catch((error) => notice(error.message, true));

