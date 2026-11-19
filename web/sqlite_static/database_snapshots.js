const databaseSnapshots = {
  source:null, selected:null,
  async open(){
    if(!el('snapshot-upload')){
      const button=node('button','上传本地快照','secondary');button.id='snapshot-upload';button.type='button';
      button.onclick=()=>run(()=>databaseUploads.open());el('snapshot-create').parentElement.append(button);
    }
    this.source=state.database;this.selected=null;
    el('snapshot-source').textContent=this.source?'当前数据库：'+this.source:'未选择数据库；仍可恢复已有快照';
    el('snapshot-create').disabled=!this.source;
    el('snapshot-target-name').value='';el('snapshot-target-label').value='';
    el('snapshot-status').textContent='';el('snapshot-restore').disabled=true;
    el('snapshot-restore-caption').textContent='选择一份快照，再恢复为新数据库。';
    el('snapshot-close').onclick=()=>el('database-snapshots-dialog').close();
    el('snapshot-refresh').onclick=()=>this.action(()=>this.refresh());
    el('snapshot-create').onclick=()=>this.action(async()=>{
      const result=await request('database-snapshots?database='+encodeURIComponent(this.source),'POST',{});
      await this.refresh();el('snapshot-status').textContent=`快照已生成：${result.filename}，${formatBytes(result.bytes)}`;
    });
    el('snapshot-restore-form').onsubmit=event=>{
      event.preventDefault();if(!this.selected)return;
      const item=this.selected,name=el('snapshot-target-name').value.trim(),label=el('snapshot-target-label').value.trim();
      this.action(async()=>{
        const result=await request(`database-snapshots/${item.id}/restore?database=${encodeURIComponent(item.database)}`,'POST',{name,chn_name:label});
        await refreshTables();
        el('snapshot-status').textContent=`已恢复为 ${result.filename}，可从左侧数据库下拉框选择。${result.definition_warning||''}`;
      });
    };
    await this.refresh();el('database-snapshots-dialog').showModal();
  },
  async action(callback){
    if(state.busy)return;
    const controls=[...el('database-snapshots-dialog').querySelectorAll('button,input')];
    const previous=controls.map(control=>control.disabled);controls.forEach(control=>control.disabled=true);
    el('snapshot-status').textContent='正在处理…';
    try{await run(callback,'snapshot-status');}
    finally{controls.forEach((control,index)=>control.disabled=previous[index]);el('snapshot-restore').disabled=!this.selected;}
  },
  async refresh(){
    const response=await workspaceFetch('/api/manage/database-snapshots'),data=await response.json();
    if(!response.ok)throw Error(data.error||'读取快照失败');
    const host=el('snapshot-list');host.replaceChildren();
    if(!data.snapshots.length){host.append(node('p','尚无快照。选择数据库后生成第一份快照。','muted'));return;}
    const table=node('table'),head=node('tr');
    ['时间 / 来源数据库','大小 / 表数','操作'].forEach(text=>head.append(node('th',text)));table.append(head);
    for(const item of data.snapshots){
      const row=node('tr'),description=node('td'),size=node('td'),actions=node('td');
      description.append(node('div',new Date(item.created).toLocaleString()),node('small',item.database));
      description.title='SHA-256: '+item.sha256;
      size.textContent=`${formatBytes(item.bytes)} / ${item.table_count} 表`;
      const link=node('a','下载','secondary small');
      link.href=workspaceUrl(`/api/manage/database-snapshots/${item.id}/download?database=${encodeURIComponent(item.database)}`);
      link.download=item.filename;link.title='下载已生成的 SQLite 快照（支持 HTTP Range）';
      const select=node('button','选为恢复源','secondary small');select.type='button';
      select.onclick=()=>{
        this.selected=item;el('snapshot-restore').disabled=false;
        host.querySelectorAll('tr').forEach(tr=>tr.classList.remove('snapshot-selected'));row.classList.add('snapshot-selected');
        el('snapshot-restore-caption').textContent='恢复源：'+item.filename+'；目标必须为未使用的新数据库名。';
        if(!el('snapshot-target-name').value)el('snapshot-target-name').value=item.database.replace(/\.sqlite3$/,'')+'_copy';
      };
      if(this.selected?.id===item.id)row.classList.add('snapshot-selected');
      actions.append(link,select);row.append(description,size,actions);table.append(row);
    }
    host.append(table);
    if(data.total>data.snapshots.length)host.append(node('p',`显示最新 ${data.snapshots.length} / ${data.total} 份；其余文件仍保留在服务端。`,'muted'));
    el('snapshot-status').textContent=`共 ${data.total} 份快照。`;
  }
};
