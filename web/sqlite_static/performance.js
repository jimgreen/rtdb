// Bounded, local-only measurements. No SQL text or record values are retained.
const uiPerformance = {
  samples: [], latest: null,
  begin(label) {
    return {label, database: state.database, table: state.table, started: performance.now(),
      at: new Date().toISOString(), request_ms:0, parse_ms:0, grid_ms:0, chart_ms:0, server_ms:null, mode:'full'};
  },
  measure(sample, key, action) {
    const start=performance.now();
    try { return action(); } finally { sample[key] += performance.now()-start; }
  },
  finish(sample, error) {
    sample.ready_ms=performance.now()-sample.started; sample.ok=!error;
    sample.rows=state.records.length; sample.columns=visibleGridColumns().length;
    this.samples.push(sample); this.samples.splice(0, Math.max(0,this.samples.length-80)); this.latest=sample;
    const target=document.getElementById('last-refresh');
    target.textContent=`${sample.ok?'更新于':'查询失败于'} ${new Date().toLocaleTimeString()} · 请求 ${sample.request_ms.toFixed(1)} ms · 界面 ${sample.ready_ms.toFixed(1)} ms`;
    target.title='双击查看性能统计；界面时间包含请求、解析与同步绘制；下一帧另列';
    let finished=false;
    const finish=(framed=false)=>{
      if(finished)return; finished=true;clearTimeout(timer);
      sample.frame_ms=document.hidden||!framed?null:performance.now()-sample.started;
    };
    const timer=setTimeout(finish,150);
    requestAnimationFrame(()=>requestAnimationFrame(()=>finish(true)));
  },
  show() {
    const host=document.getElementById('performance-results');host.replaceChildren();
    const table=node('table'), head=node('tr');
    ['时间','操作 / 刷新方式','请求','服务处理','排队','执行','打开/等锁','保存校验','数据写入','回执写入','事务合计','SQL/取数','解码','版本','审计','响应编码','解析','表格','曲线','界面完成','下一帧'].forEach(text=>head.append(node('th',text)));
    const thead=node('thead');thead.append(head);table.append(thead);
    const body=node('tbody'), format=value=>value==null?'—':Number(value).toFixed(1);
    for(const s of [...this.samples].reverse()) {
      const row=node('tr');
      [new Date(s.at).toLocaleTimeString(),`${s.label} / ${{full:'完整',patch:'局部',unchanged:'无变化'}[s.mode]||s.mode}${s.pagination==='cursor'?' / 游标':''}${s.ok?'':'（失败）'}`,
        ...['request_ms','server_ms','queue_ms','execute_ms','open_ms','validate_ms','write_ms','receipt_ms','transaction_ms','query_ms','decode_ms','version_ms','audit_ms','encode_ms','parse_ms','grid_ms','chart_ms','ready_ms','frame_ms'].map(k=>format(s[k]))].forEach(value=>row.append(node('td',value)));
      body.append(row);
    }
    table.append(body);host.append(table);document.getElementById('performance-dialog').showModal();
  },
  export() { download(JSON.stringify({unit:'ms',scope:'browser request through DOM update; frame_ms is a second animation-frame estimate, not display latency',samples:this.samples.map(({started,...sample})=>sample)},null,2),'rtdb-ui-performance.json','application/json'); }
};

function flashGridCellChange(cell, prevVal, nextVal) {
  if (!cell) return;
  cell.classList.remove("cell-flash-up", "cell-flash-down", "cell-flash-chg");
  const prevNum = typeof prevVal === "number" ? prevVal : (typeof prevVal === "string" && prevVal.trim() !== "" && Number.isFinite(Number(prevVal)) ? Number(prevVal) : null);
  const nextNum = typeof nextVal === "number" ? nextVal : (typeof nextVal === "string" && nextVal.trim() !== "" && Number.isFinite(Number(nextVal)) ? Number(nextVal) : null);
  const cls = (prevNum !== null && nextNum !== null && prevNum !== nextNum)
    ? (nextNum > prevNum ? "cell-flash-up" : "cell-flash-down")
    : "cell-flash-chg";
  void cell.offsetWidth;
  cell.classList.add(cls);
  clearTimeout(cell._flashTimer);
  cell._flashTimer = setTimeout(() => { cell.classList.remove(cls); }, 1250);
}

function patchGridRecords(incoming) {
  if(incoming.length!==state.records.length || document.querySelector('#data-grid .cell-editor') || state.records.some(r=>r.draftId)) return false;
  for(let i=0;i<incoming.length;i++) {
    if(JSON.stringify(incoming[i].ref.key)!==JSON.stringify(state.records[i].ref.key) || !virtualGrid.active&&!gridCells.has(state.records[i])) return false;
  }
  const columns=visibleGridColumns();
  for(let i=0;i<incoming.length;i++) {
    const existing=state.records[i], next=incoming[i];
    if(existing.ref.version===next.ref.version)continue;
    const cellMap = gridCells.get(existing);
    const changes = [];
    for (const c of columns) {
      const cell = cellMap?.get(c.eng_name);
      if (!cell) continue;
      const prevVal = existing.row[c.eng_name];
      const nextVal = next.row[c.eng_name];
      const valChanged = !valuesEqual(prevVal, nextVal);
      if (valChanged || cell.classList.contains('cell-modified')) {
        changes.push({ column: c, cell, prevVal, nextVal, valChanged });
      }
    }
    existing.row=next.row; existing.ref=next.ref; existing.baseline=structuredClone(next.row);
    for(const item of changes) {
      renderGridCellValue(existing, item.column, item.cell);
      if (item.valChanged) flashGridCellChange(item.cell, item.prevVal, item.nextVal);
    }
  }
  return true;
}
