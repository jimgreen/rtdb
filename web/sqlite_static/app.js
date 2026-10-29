'use strict';
const state = {tables: [], table: null, rows: [], total: 0, offset: 0, limit: 100, versions: [], selected: new Set(), logs: [], queryId: 0, loadId: 0, editing: null};
const $ = id => document.getElementById(id), enc = encodeURIComponent;
const current = () => state.tables.find(t => t.name === state.table);
const endpoint = suffix => `/api/sqlite/tables/${enc(state.table)}/${suffix}`;
const esc = v => String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const ints = new Set(['int','integer','long']), floats = new Set(['float','double','decimal','number']);
const objects = new Set(['dict','object','json','list','array']), bools = new Set(['bool','boolean']);
async function api(url, method = 'GET', payload) {
  const response = await fetch(url, {method, headers: {'Content-Type': 'application/json'}, body: payload === undefined ? undefined : JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || `请求失败 ${response.status}`);
  return data;
}
function log(message) {
  state.logs.unshift(`${new Date().toLocaleTimeString()}  ${message}`);
  state.logs = state.logs.slice(0, 500); $('logs').textContent = state.logs.join('\n');
}
function notice(message, error = false) {
  $('notice').hidden = !message; $('notice').textContent = message; $('notice').className = `notice${error ? ' error' : ''}`;
}
function fail(error) { notice(error.message, true); log(`失败：${error.message}`); }
function activate(tab) {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${tab}`));
}
function controls() {
  for (const id of ['add-row','import','export','clear-table','check-table','drop-table','run-sql']) $(id).disabled = !state.table;
  $('delete-rows').disabled = !state.selected.size;
  $('selection-summary').textContent = state.selected.size ? `已选择 ${state.selected.size} 条记录` : '未选择记录';
  $('prev').disabled = state.offset === 0; $('next').disabled = state.offset + state.limit >= state.total;
}
async function loadTables(select = state.table) {
  const ticket = ++state.loadId, result = await api('/api/sqlite/tables');
  if (ticket !== state.loadId) return;
  state.tables = result.tables; $('table-count').textContent = `${state.tables.length} 张表`; renderTables();
  const table = state.tables.find(t => t.name === select) || state.tables[0];
  if (table) await selectTable(table.name); else newTable();
}
function renderTables() {
  const q = $('table-filter').value.trim().toLowerCase(), list = $('table-list'); list.replaceChildren();
  for (const t of state.tables.filter(t => (!q || `${t.name} ${t.definition.head.chn_name}`.toLowerCase().includes(q)) && (!$('nonempty').checked || t.row_count > 0))) {
    const b = document.createElement('button'); b.className = `table-item${t.name === state.table ? ' active' : ''}`;
    b.innerHTML = `<span>${esc(t.definition.head.chn_name)}<small> · ${esc(t.name)}</small></span><small>${t.row_count ?? '?'}</small>`;
    b.onclick = () => selectTable(t.name).catch(fail); list.append(b);
  }
  if (!list.children.length) list.innerHTML = '<p class="muted">没有匹配的表。</p>';
}
async function selectTable(name) {
  if (name !== state.table) { state.offset = 0; $('where').value = ''; $('order').value = ''; }
  state.table = name; const t = current();
  $('current-table').textContent = `${t.definition.head.chn_name} · ${name}`;
  $('table-meta').textContent = `${t.row_count ?? '?'} 行 · 主键 ${t.definition.columns.filter(c => c.primary_key).map(c => c.eng_name).join(' + ')}`;
  $('schema-editor').value = JSON.stringify(t.definition, null, 2);
  $('field-list').innerHTML = t.definition.columns.map(c => `<div><strong>${c.primary_key ? '◆ ' : ''}${esc(c.chn_name)}</strong><small>${esc(c.eng_name)} · ${esc(c.data_type)}</small></div>`).join('');
  renderTables(); controls();
  if (t.error) { ++state.queryId; state.rows = []; state.selected.clear(); renderGrid(); throw Error(t.error); }
  await query();
}
function newTable() {
  ++state.queryId; state.table = null; state.rows = []; state.selected.clear(); state.total = 0; state.offset = 0;
  $('current-table').textContent = '新建 SQLite 表'; $('table-meta').textContent = '修改英文名、中文名和字段后保存';
  $('schema-editor').value = JSON.stringify({head: {chn_name:'新数据表',eng_name:'new_table'}, columns:[
    {chn_name:'编号',eng_name:'id',primary_key:true,data_type:'int',disp_type:'数字',disp_menu:''},
    {chn_name:'名称',eng_name:'name',primary_key:false,data_type:'str',disp_type:'文本',disp_menu:''}]}, null, 2);
  $('field-list').replaceChildren(); renderGrid(); renderTables(); activate('schema');
}
async function query() {
  if (!state.table) return;
  const ticket = ++state.queryId, name = state.table; state.selected.clear(); controls();
  state.limit = Number($('page-size').value);
  const qs = new URLSearchParams({limit:state.limit,offset:state.offset,where:$('where').value.trim(),order:$('order').value.trim()});
  const p = await api(`${endpoint('rows')}?${qs}`);
  if (ticket !== state.queryId || name !== state.table) return;
  state.rows = p.rows; state.total = p.total; state.versions = p.versions;
  if (!p.rows.length && p.total && state.offset) { state.offset = Math.floor((p.total - 1) / state.limit) * state.limit; return query(); }
  renderGrid(); $('query-summary').textContent = `共 ${p.total} 行 · 当前 ${p.rows.length} 行`;
  $('page').textContent = `第 ${Math.floor(state.offset / state.limit) + 1} / ${Math.max(1, Math.ceil(p.total / state.limit))} 页`;
  log(`查询 ${name}：${p.rows.length}/${p.total}`);
}
function renderGrid() {
  const box = $('grid'); box.replaceChildren();
  if (!state.rows.length) { box.innerHTML = '<p class="empty">暂无记录。可新增记录或导入 CSV / JSON。</p>'; controls(); return; }
  const cols = current().definition.columns, table = document.createElement('table');
  table.innerHTML = `<thead><tr><th><input id="select-page" type="checkbox" aria-label="选择当前页"> 操作</th>${cols.map(c => `<th>${esc(c.chn_name)}${c.primary_key ? ' ◆' : ''}<small>${esc(c.eng_name)}</small></th>`).join('')}</tr></thead>`;
  const body = document.createElement('tbody');
  state.rows.forEach((row, i) => {
    const tr = document.createElement('tr'), actions = document.createElement('td'); actions.className = 'row-actions';
    actions.innerHTML = `<input type="checkbox" aria-label="选择第 ${i + 1} 条"><button class="quiet">编辑</button><button class="quiet">复制</button>`;
    actions.children[0].checked = state.selected.has(i);
    actions.children[0].onchange = e => { e.target.checked ? state.selected.add(i) : state.selected.delete(i); controls(); };
    actions.children[1].onclick = () => openRow(i); actions.children[2].onclick = () => openRow(null, row); tr.append(actions);
    for (const col of cols) { const td = document.createElement('td'), v = row[col.eng_name]; td.textContent = v == null ? 'NULL' : typeof v === 'object' ? JSON.stringify(v) : String(v); td.title = td.textContent; tr.append(td); }
    body.append(tr);
  });
  table.append(body); box.append(table);
  $('select-page').onchange = e => { state.selected = new Set(e.target.checked ? state.rows.map((_, i) => i) : []); renderGrid(); };
  $('select-page').checked = state.selected.size === state.rows.length; controls();
}
function openRow(index, copy) {
  if (!state.table) return;
  const row = {...(copy || (index == null ? {} : state.rows[index]))}, columns = current().definition.columns;
  if (copy) for (const c of columns.filter(c => c.primary_key)) delete row[c.eng_name];
  state.editing = {table:state.table,row,columns,key:index == null ? null : columns.filter(c => c.primary_key).map(c => row[c.eng_name]),version:state.versions[index]};
  $('dialog-title').textContent = index == null ? (copy ? '复制记录' : '新增记录') : '编辑记录';
  $('dialog-key').textContent = index == null ? '填写主键；可为空的字段勾选 NULL。' : '主键保持不变。保存前会检测其他进程是否修改了该记录。';
  $('json-mode').checked = false; $('row-editor').hidden = true; $('row-fields').hidden = false;
  $('row-editor').value = JSON.stringify(row, null, 2); $('row-error').textContent = ''; renderFields(row); $('row-dialog').showModal();
}
function renderFields(row) {
  const box = $('row-fields'); box.replaceChildren();
  state.editing.columns.forEach((c, i) => {
    const line = document.createElement('div'); line.className = 'record-field';
    const label = document.createElement('label'); label.htmlFor = `value-${i}`; label.textContent = `${c.chn_name} · ${c.eng_name}${c.primary_key ? ' ◆' : ''}`;
    const kind = c.data_type.toLowerCase(), input = document.createElement(bools.has(kind) || c.disp_menu ? 'select' : objects.has(kind) ? 'textarea' : 'input'); input.id = `value-${i}`;
    if (bools.has(kind)) input.innerHTML = '<option value="true">true · 是</option><option value="false">false · 否</option>';
    else if (c.disp_menu) for (const value of [...new Set([...(row[c.eng_name] == null ? [] : [String(row[c.eng_name])]), ...c.disp_menu.split('|')])]) { const option = document.createElement('option'); option.value = option.textContent = value; input.append(option); }
    if (row[c.eng_name] != null) input.value = objects.has(kind) ? JSON.stringify(row[c.eng_name]) : String(row[c.eng_name]);
    if (ints.has(kind)) input.inputMode = 'numeric'; if (floats.has(kind)) input.inputMode = 'decimal';
    const nullLabel = document.createElement('label'); nullLabel.className = 'check-line';
    const nil = document.createElement('input'); nil.type = 'checkbox'; nil.id = `null-${i}`; nil.checked = row[c.eng_name] == null && !c.primary_key; nil.disabled = c.primary_key;
    const fixed = !!state.editing.key && c.primary_key; input.disabled = nil.checked || fixed; nil.onchange = () => input.disabled = nil.checked || fixed;
    nullLabel.append(nil, ' NULL'); line.append(label, input, nullLabel); box.append(line);
  });
}
function readFields(strict = true) {
  const result = {};
  state.editing.columns.forEach((c, i) => {
    const kind = c.data_type.toLowerCase(), value = $(`value-${i}`).value;
    if ($(`null-${i}`).checked) result[c.eng_name] = null;
    else if (ints.has(kind)) { if (strict && !/^-?\d+$/.test(value)) throw Error(`${c.chn_name} 必须为整数`); result[c.eng_name] = value; }
    else if (floats.has(kind)) { if (strict && (!value.trim() || !Number.isFinite(Number(value)))) throw Error(`${c.chn_name} 必须为有限数值`); result[c.eng_name] = Number(value); }
    else if (bools.has(kind) || objects.has(kind)) result[c.eng_name] = JSON.parse(value);
    else result[c.eng_name] = value;
  }); return result;
}
async function saveRow() {
  $('save-row').disabled = true;
  try {
    const values = $('json-mode').checked ? JSON.parse($('row-editor').value) : readFields(), edit = state.editing, base = `/api/sqlite/tables/${enc(edit.table)}/`;
    await api(base + (edit.key ? 'row' : 'rows'), edit.key ? 'PUT' : 'POST', edit.key ? {key:edit.key,values,version:edit.version} : {rows:[values]});
    $('row-dialog').close(); await loadTables(edit.table); notice('记录已保存'); log(`保存记录 ${edit.table}`);
  } catch (e) { $('row-error').textContent = e.message; fail(e); } finally { $('save-row').disabled = false; }
}
function confirmOperation(message, name = state.table) {
  const dialog = $('confirm-dialog'); $('confirm-text').textContent = `${message}。请输入完整表名 ${name} 确认。`;
  $('confirm-input').value = ''; $('confirm-action').disabled = true;
  $('confirm-input').oninput = () => $('confirm-action').disabled = $('confirm-input').value !== name;
  dialog.returnValue = ''; dialog.showModal(); $('confirm-input').focus();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirmed' && $('confirm-input').value === name), {once:true}));
}
async function deleteSelected() {
  if (!state.selected.size || !await confirmOperation(`删除选中的 ${state.selected.size} 条记录`)) return;
  const cols = current().definition.columns.filter(c => c.primary_key), records = [...state.selected].map(i => ({key:cols.map(c => state.rows[i][c.eng_name]),version:state.versions[i]}));
  await api(endpoint('rows'), 'DELETE', {records}); await loadTables(); notice(`已删除 ${records.length} 条记录`); log('删除选中记录');
}
async function saveSchema() {
  const definition = JSON.parse($('schema-editor').value), old = current();
  if (old && definition.head?.eng_name !== old.name) throw Error('现有表不可直接重命名，请新建表并导入数据');
  if (old && !await confirmOperation('修改表结构；请先停止其他进程对该表的访问')) return;
  await api(old ? endpoint('schema') : '/api/sqlite/schemas', old ? 'PUT' : 'POST', {definition,version:old?.schema_version});
  await loadTables(definition.head.eng_name); notice('表结构已保存'); log('保存表结构');
}
async function clearTable() {
  if (!state.table || !await confirmOperation('清空整张表，所有记录将被删除')) return;
  await api(endpoint('clear'), 'POST', {confirm:state.table,expected_count:current().row_count}); state.offset = 0; await loadTables(); notice('表已清空'); log('清空表');
}
async function dropTable() {
  if (!state.table || !await confirmOperation('删除空表结构并归档数据库文件；请先停止其他进程访问')) return;
  const p = await api(endpoint('table'), 'DELETE', {confirm:state.table,version:current().schema_version}); await loadTables(null); notice(`空表已归档：${p.archive}`); log('删除空表');
}
async function runSql() {
  const sql = $('sql-editor').value.trim(), write = /^(INSERT|UPDATE|DELETE)\b/i.test(sql);
  if (write && !await confirmOperation('执行 SQL 写入，以当前数据库内容为准')) return;
  const p = await api('/api/sqlite/sql', 'POST', {table:state.table,sql,confirm:write ? state.table : undefined}), box = $('sql-result'); box.replaceChildren();
  const info = document.createElement('p'); info.className = 'muted'; info.textContent = `${p.readonly ? '返回' : '影响'} ${p.rowcount} 行 · ${p.elapsed_ms} ms${p.truncated ? ' · 结果截断为 500 行' : ''}`; box.append(info);
  if (p.rows?.length) {
    const cols = p.columns, table = document.createElement('table'); table.innerHTML = `<tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr>`;
    for (const row of p.rows) { const tr = document.createElement('tr'); for (const c of cols) { const td = document.createElement('td'); td.textContent = typeof row[c] === 'object' ? JSON.stringify(row[c]) : String(row[c]); tr.append(td); } table.append(tr); } box.append(table);
  }
  log(`SQL ${p.rowcount} 行，${p.elapsed_ms} ms`); if (write) await loadTables();
}
async function exportData() {
  const format = $('export-format').value, qs = new URLSearchParams({format,where:$('where').value,order:$('order').value});
  const response = await fetch(`${endpoint('export')}?${qs}`); if (!response.ok) throw Error((await response.json()).error);
  const url = URL.createObjectURL(await response.blob()), link = document.createElement('a'); link.href = url; link.download = `${state.table}.${format}`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); log(`导出 ${state.table}.${format}`);
}
async function importData(file) {
  const format = file.name.toLowerCase().endsWith('.csv') ? 'csv' : 'json';
  const p = await api(endpoint('import'), 'POST', {format,text:await file.text()}); await loadTables(); notice(`导入完成：${p.inserted} 条记录`); log(`导入 ${file.name}`);
}
function sqlTemplate() {
  const t = current(); if (!t) return;
  const name = `"${t.name}"`, cols = t.definition.columns, key = cols.filter(c => c.primary_key).map(c => `"${c.eng_name}" = ${ints.has(c.data_type) ? '1' : "'key'"}`).join(' AND '), field = cols.find(c => !c.primary_key) || cols[0];
  const templates = {select:`SELECT * FROM ${name} ORDER BY "${cols[0].eng_name}" LIMIT 100`,count:`SELECT COUNT(*) AS count FROM ${name}`,delete:`DELETE FROM ${name} WHERE ${key}`,update:`UPDATE ${name} SET "${field.eng_name}" = ${ints.has(field.data_type) || floats.has(field.data_type) ? '0' : "'new value'"} WHERE ${key}`,insert:`INSERT INTO ${name} (${cols.map(c => `"${c.eng_name}"`).join(', ')}) VALUES (${cols.map(c => ints.has(c.data_type) || floats.has(c.data_type) || bools.has(c.data_type) ? '1' : "'value'").join(', ')})`};
  $('sql-editor').value = templates[$('sql-template').value] || '';
}
function setup() {
  const bind = (id, fn) => $(id).onclick = () => Promise.resolve().then(fn).catch(fail);
  document.querySelectorAll('.tab').forEach(b => b.onclick = () => activate(b.dataset.tab));
  bind('reload', () => loadTables()); bind('refresh-tables', () => loadTables()); $('table-filter').oninput = renderTables; $('nonempty').onchange = renderTables;
  bind('query', () => { state.offset = 0; return query(); }); bind('clear-query', () => { state.offset = 0; $('where').value = ''; $('order').value = ''; return query(); });
  bind('prev', () => { state.offset = Math.max(0, state.offset - state.limit); return query(); }); bind('next', () => { if (state.offset + state.limit < state.total) state.offset += state.limit; return query(); });
  $('page-size').onchange = () => { state.offset = 0; query().catch(fail); };
  bind('add-row', () => openRow(null)); bind('save-row', saveRow); bind('delete-rows', deleteSelected); bind('save-schema', saveSchema); bind('clear-table', clearTable); bind('drop-table', dropTable); bind('run-sql', runSql); bind('export', exportData); bind('import', () => $('file-input').click());
  $('file-input').onchange = async e => { try { if (e.target.files[0]) await importData(e.target.files[0]); } catch (error) { fail(error); } finally { e.target.value = ''; } };
  bind('clear-logs', () => { state.logs = []; $('logs').textContent = ''; });
  bind('health', async () => { const p = await api('/api/health'); notice(`SQLite ${p.sqlite_version} · ${p.durability} · ${p.data_dir}`); });
  bind('check-table', async () => { const p = await api(endpoint('check')); notice(`完整性检查：${p.ok ? '通过' : '失败'}；${p.row_count} 条记录；${p.integrity.join(', ')}`, !p.ok); log(`完整性检查 ${p.ok ? '通过' : '失败'}`); });
  bind('new-table', newTable); $('sql-template').onchange = sqlTemplate;
  $('json-mode').onchange = () => { try { if ($('json-mode').checked) $('row-editor').value = JSON.stringify(readFields(false), null, 2); else renderFields(JSON.parse($('row-editor').value)); $('row-editor').hidden = !$('json-mode').checked; $('row-fields').hidden = $('json-mode').checked; } catch (e) { $('json-mode').checked = !$('json-mode').checked; $('row-error').textContent = e.message; } };
  controls(); loadTables().catch(fail);
}
setup();
