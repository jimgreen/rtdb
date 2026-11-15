// Local edit transactions. No server writes occur until the existing save flow.
let editUndo = [], editRedo = [];
function resetEditHistory() { editUndo = []; editRedo = []; }
function editSnapshot() {
  const records = [...state.records], edits = [...state.pendingEdits.values()].map((edit) => ({...edit}));
  return {records, edits, signature: JSON.stringify([records.map(recordKey), edits.map((edit) => [cellKey(edit.record, edit.column.eng_name), edit.raw])])};
}
function rememberEdit(before) {
  if (!before || before.signature === editSnapshot().signature) return;
  editUndo.push(before); editRedo = [];
  while (editUndo.length > 50 || editUndo.length > 1 && editUndo.reduce((sum, item) => sum + item.edits.length, 0) > 30000) editUndo.shift();
  localDrafts.schedule();
}
function restoreEdit(redo = false) {
  const source = redo ? editRedo : editUndo, destination = redo ? editUndo : editRedo;
  if (!source.length) { notice(redo ? "没有可重做的修改" : "没有可撤销的修改"); return; }
  destination.push(editSnapshot()); const snapshot = source.pop();
  state.records = [...snapshot.records]; state.pendingEdits.clear();
  for (const record of state.records) record.row = structuredClone(record.baseline);
  for (const edit of snapshot.edits) setPendingCell(edit.record, edit.column, edit.raw, false);
  clearGridSelection(); rebindAddedChartSeries(); renderGrid(); drawChart();
  localDrafts.schedule();
  notice(redo ? "已重做，保存后写入数据库" : "已撤销上一步修改");
}
function locateEdit(record, field) {
  const row = state.records.indexOf(record);
  if (row < 0) return;
  if (!visibleGridColumns().some((column) => column.eng_name === field)) { state.fields.add(field); renderFields(); renderGrid(); }
  tab("data");
  const target = virtualGrid.ensure(row,visibleGridColumns().findIndex(column=>column.eng_name===field));
  if (target) {
    selectGridCells(row, Number(target.dataset.cellColumn), row, Number(target.dataset.cellColumn));
    target.scrollIntoView({block:"center", inline:"nearest"}); target.focus({preventScroll:true});
  }
}
function previewEdits() {
  if (!hasPendingData()) { notice("没有待保存修改"); return; }
  const table = node("table"), head = node("tr"), thead = node("thead"), body = node("tbody");
  for (const title of ["记录 / KEY", "字段", "原值", "新值"]) head.append(node("th", title));
  thead.append(head); table.append(thead);
  const changes = [...state.pendingEdits.values()];
  for (const record of draftRecords()) for (const column of state.definition.columns)
    if (!pendingCell(record, column.eng_name)) changes.push({record, column, raw:cellText(record.row[column.eng_name])});
  for (const {record, column, raw} of changes) {
    const row = node("tr"); row.tabIndex = 0; row.title = "点击定位单元格";
    for (const value of [record.draftId ? `新增 ${record.draftId}` : record.ref.key.join(" / "),
      `${chineseName(column)} (${column.eng_name})`, record.draftId ? "新增" : display(record.baseline[column.eng_name]), raw === "" ? "空值" : raw]) row.append(node("td", value));
    const locate = () => { el("edit-preview-dialog").close(); locateEdit(record, column.eng_name); };
    row.onclick = locate; row.onkeydown = (event) => { if (event.key === "Enter") locate(); }; body.append(row);
  }
  table.append(body); el("edit-preview-grid").replaceChildren(table);
  el("edit-preview-caption").textContent = `${state.database} / ${state.table} · ${changes.length} 个字段；点击行定位，确认后保存。`;
  el("edit-preview-dialog").showModal();
}

let conflictChoices = [];
function renderSaveProblems(error) {
  const grid = el("save-conflict-grid"), apply = el("apply-conflict-choices");
  grid.replaceChildren(); apply.hidden = true; apply.disabled = true; conflictChoices = [];
  const find = (key) => state.records.find((record) => !record.draftId && JSON.stringify(record.ref?.key) === JSON.stringify(key));
  const issues = error.details?.issues || (error.editTarget ? [{message:error.message, target:error.editTarget}] : []);
  if (issues.length) {
    el("save-warning-list").replaceChildren();
    for (const issue of issues) {
      const record = issue.target?.record || (issue.draft_index != null ? draftRecords()[issue.draft_index] : find(issue.key));
      const field = issue.target?.field || issue.fields?.[0], item = node("li", issue.message + " ");
      if (record && field) {
        const button = node("button", "定位单元格", "secondary small"); button.type = "button";
        button.onclick = () => { el("save-warning-dialog").close(); locateEdit(record, field); };
        item.append(button);
      }
      el("save-warning-list").append(item);
    }
  }
  const conflicts = error.details?.conflicts || [];
  if (!conflicts.length) return;
  const table = node("table"), head = node("tr"), thead = node("thead"), tbody = node("tbody");
  for (const label of ["KEY / 字段", "原值", "服务器值", "本地修改值", "解决方式"]) head.append(node("th", label));
  thead.append(head); table.append(thead);
  const update = () => { apply.disabled = !conflictChoices.length || conflictChoices.some((item) => item.controls.some((control) => !control.value)); };
  for (const conflict of conflicts) {
    const record = find(conflict.key); if (!record) continue;
    const item = {record, conflict, controls:[]}; conflictChoices.push(item);
    for (const field of conflict.fields) {
      const row = node("tr"), column = state.definition.columns.find((entry) => entry.eng_name === field);
      const raw = pendingCell(record, field)?.raw ?? cellText(record.row[field]);
      for (const value of [`${conflict.key.join(" / ")} / ${column ? chineseName(column) : field}`, display(record.baseline[field]),
        conflict.current ? display(conflict.current[field]) : "记录已删除", raw]) row.append(node("td", value));
      const choice = node("select"); choice.setAttribute("aria-label", `冲突 ${conflict.key.join(" / ")} ${field}`); choice.dataset.field = field;
      choice.append(new Option("请选择…", ""));
      if (conflict.current) choice.append(new Option("保留本地修改", "local"), new Option("采用服务器值", "server"));
      else choice.append(new Option("放弃此行本地修改", "discard"));
      choice.onchange = update; item.controls.push(choice);
      const cell = node("td"); cell.append(choice); row.append(cell); tbody.append(row);
    }
  }
  table.append(tbody); grid.append(table); apply.hidden = false; apply.onclick = applyConflictChoices; update();
}

function applyConflictChoices() {
  if (el("apply-conflict-choices").disabled || state.busy) return;
  for (const {record, conflict, controls} of conflictChoices) {
    const pending = [...state.pendingEdits.values()].filter((edit) => edit.record === record);
    for (const edit of pending) state.pendingEdits.delete(cellKey(record, edit.column.eng_name));
    if (!conflict.current) {
      state.records = state.records.filter((item) => item !== record);
      state.total = Math.max(0, state.total-1); state.tableTotal = Math.max(0, state.tableTotal-1); continue;
    }
    record.baseline = structuredClone(conflict.current); record.row = structuredClone(conflict.current); record.ref = conflict.current_ref;
    const choices = new Map(controls.map((control) => [control.dataset.field, control.value]));
    for (const edit of pending) if (choices.get(edit.column.eng_name) !== "server") setPendingCell(record, edit.column, edit.raw, false);
  }
  resetEditHistory(); state.queryVersion = null; clearGridSelection(); rebindAddedChartSeries(); renderGrid(); drawChart();
  conflictChoices = []; el("save-warning-dialog").close();
  notice(hasPendingData() ? "已应用冲突处理选择，请检查后 Ctrl+S 保存" : "已采用服务器数据，当前没有待保存修改");
  localDrafts.schedule();
}
