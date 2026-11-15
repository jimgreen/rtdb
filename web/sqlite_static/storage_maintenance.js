const storageMaintenance = {
  preview: null, busy: false,
  names: {uploads:'上传暂存', snapshots:'整库快照', tasks:'任务账本', logs:'日期日志'},
  async open() {
    const dialog=el('storage-maintenance-dialog');
    el('storage-close').onclick=()=>dialog.close();
    el('storage-refresh').onclick=()=>this.perform(()=>this.refresh());
    el('storage-policy').onsubmit=event=>{event.preventDefault();this.perform(async()=>{
      const config=Object.fromEntries(Array.from(new FormData(event.target),([k,v])=>[k,Number(v)]));
      await request('storage-maintenance','PUT',config);await this.refresh();
      el('storage-status').textContent='保留配置已保存；未执行清理。';
    });};
    el('storage-policy').oninput=()=>{this.preview=null;el('storage-clean').disabled=true;};
    el('storage-clean').onclick=()=>this.perform(()=>this.clean());
    dialog.showModal();await this.perform(()=>this.refresh());
  },
  async perform(action) {
    if(this.busy)return;this.busy=true;
    for(const id of ['storage-refresh','storage-save','storage-clean'])el(id).disabled=true;
    try { await action(); }
    catch(error) { this.preview=null;el('storage-status').textContent=error.message;notice(error.message,true); }
    finally {
      this.busy=false;el('storage-refresh').disabled=false;el('storage-save').disabled=false;
      el('storage-clean').disabled=!this.preview?.candidates.length;
    }
  },
  async refresh() {
    this.preview=null;el('storage-status').textContent='正在统计…';
    const result=await request('storage-maintenance');this.preview=result;
    for(const [key,value] of Object.entries(result.config))el('storage-policy').elements.namedItem(key).value=value;
    const stats=el('storage-statistics');stats.replaceChildren();
    for(const [kind,item] of Object.entries(result.statistics)) {
      const card=node('div');card.append(node('strong',this.names[kind]),node('span',formatBytes(item.bytes)),
        node('small',kind==='tasks'?`${item.records||0} 个任务 · ${item.active||0} 个活动任务`:`${item.files} 个文件`));stats.append(card);
    }
    const days=result.statistics.logs.retention_days;
    el('storage-log-policy').textContent=`日志：${days?'保留 '+days+' 天':'永久保留'}，沿用“日志”页的查询和清理设置。账本清理释放的页供后续写入复用，文件不一定立即缩小。`;
    const list=el('storage-candidates');list.replaceChildren();
    if(!result.candidates.length)list.append(node('p','没有符合保留策略的可清理内容。','muted'));
    else {
      const table=node('table'),head=node('thead'),header=node('tr'),body=node('tbody');
      for(const title of ['类型 / 名称','更新时间','占用','处理方式'])header.append(node('th',title));head.append(header);
      for(const item of result.candidates) {
        const row=node('tr'),name=node('td');name.append(node('strong',this.names[item.kind]),node('div',item.name));
        row.append(name,node('td',new Date(item.updated*1000).toLocaleString()),node('td',formatBytes(item.bytes)),node('td',item.action));body.append(row);
      }
      table.append(head,body);list.append(table);
    }
    const protectedList=el('storage-protected-list');protectedList.replaceChildren();
    for(const item of result.protected)protectedList.append(node('li',`${this.names[item.kind]} · ${item.name}：${item.reason}`));
    el('storage-protected').hidden=!result.protected_count;
    el('storage-protected').querySelector('summary').textContent=`受保护项目（${result.protected_count}）`;
    el('storage-status').textContent=`扫描 ${result.scan_ms.toFixed(1)} ms；本次可处理 ${result.candidates.length} 项${result.limited?'，其余请清理后重新预览':''}。预览有效期 10 分钟。`;
  },
  async clean() {
    const preview=this.preview;if(!preview?.candidates.length)return;
    if(!await confirmAction('确认清理服务存储',`将处理预览中的 ${preview.candidates.length} 项。上传暂存和快照删除后不可恢复；任务仅清理详情。`))return;
    this.preview=null;
    const result=await request('storage-maintenance','POST',{token:preview.token});
    await this.refresh();
    el('storage-status').textContent=`已处理 ${result.cleaned} 项，删除文件 ${formatBytes(result.deleted_file_bytes)}，清理 ${result.compacted_tasks} 个任务详情。`+
      (result.failures.length?' 失败：'+result.failures.map(item=>`${item.name}：${item.error}`).join('；'):'');
  }
};
