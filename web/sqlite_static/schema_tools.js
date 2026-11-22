// Index choices are part of the unsaved schema and its recoverable local draft.
const schemaTools={
  openIndexes(){
    const definition=schemaFormValue(),fields=definition.columns.map(c=>c.eng_name);
    this.fields=fields;el('index-mode').value=state.schemaIndexes==null?'auto':'custom';
    el('index-rows').replaceChildren();
    for(const columns of definition.indexes||[])this.addIndex(columns);
    this.updateMode();el('schema-index-dialog').showModal();
  },
  updateMode(){el('index-custom').hidden=el('index-mode').value==='auto';},
  addIndex(fields=[]){
    const row=node('div',null,'index-entry'),selected=node('span',fields.join(' → ')||'尚未选择字段');row.fields=[...fields];
    const choose=node('select');choose.setAttribute('aria-label','索引字段');
    const refreshOptions=()=>{
      choose.replaceChildren(new Option('选择字段…',''));
      for(const field of this.fields){
        if(!row.fields.includes(field))choose.append(new Option(field,field));
      }
      choose.disabled=choose.options.length<=1;
    };
    refreshOptions();
    choose.onchange=()=>{if(choose.value&&!row.fields.includes(choose.value)){row.fields.push(choose.value);selected.textContent=row.fields.join(' → ');refreshOptions();}choose.value='';};
    const back=node('button','撤回末列','secondary small');back.type='button';back.onclick=()=>{row.fields.pop();selected.textContent=row.fields.join(' → ')||'尚未选择字段';refreshOptions();};
    const remove=node('button','删除索引','danger-outline small');remove.type='button';remove.onclick=()=>row.remove();
    row.append(selected,choose,back,remove);el('index-rows').append(row);
  },
  apply(){
    const indexes=el('index-mode').value==='auto'?null:[...el('index-rows').children].map(row=>row.fields);
    if(indexes?.some(fields=>!fields.length||fields.length>8))throw Error('每个索引请选择 1 至 8 个字段；不需要的索引请删除');
    if(indexes&&new Set(indexes.map(fields=>JSON.stringify(fields))).size!==indexes.length)throw Error('索引字段组合重复');
    const original=Object.fromEntries([...el('schema-fields').querySelectorAll('tr[data-original-name]')].map(row=>[row.querySelector('[data-property="eng_name"]').value.trim(),row.dataset.originalName]));
    state.schemaIndexes=indexes?.map(fields=>fields.map(name=>original[name]||name))??null;schemaChanged();el('schema-index-dialog').close();notice('索引方案已加入待保存结构；Ctrl+S 后生效');
  },
  async preview(){
    el('schema-preview-title').textContent='结构变更预览';
    const desired=schemaFormValue(),renames={};
    el('schema-fields').querySelectorAll('tr[data-original-name]').forEach(row=>{const name=row.querySelector('[data-property="eng_name"]').value.trim();if(name!==row.dataset.originalName)renames[row.dataset.originalName]=name;});
    const result=await request(tablePath('schema-preview',state.table||desired.head.eng_name),'POST',{
      definition:desired,version:state.version,creating:!state.table,column_renames:renames});
    el('schema-preview-content').textContent=result.changes.length?result.changes.join('\n'):'没有结构变化';
    const success = isNativeDatabase()
      ? `当前 ${result.row_count} 条记录。保存时会检查原表约束和结构版本。`
      : `校验通过，当前 ${result.row_count} 条记录。保存时会再次检查版本。`;
    el('schema-preview-status').textContent=result.errors.length?'不能保存：'+result.errors.join('；'):success;
    el('schema-preview-status').classList.toggle('is-error',!!result.errors.length);el('schema-preview-dialog').showModal();
  },
  async queryPlan(){
    requireTable();const result=await request(tablePath('query-plan'),'POST',{sql:el('sql-editor').value});
    el('schema-preview-title').textContent='当前查询计划';
    el('schema-preview-content').textContent=result.plan.map(item=>`${item.id} ← ${item.parent}  ${item.detail}`).join('\n')+'\n\n字段对应：'+Object.entries(result.columns).map(([name,physical])=>`${physical} = ${name}`).join('，');
    el('schema-preview-status').textContent='按当前已保存的索引分析，不执行查询，也不包含待保存的索引方案。';
    el('schema-preview-status').classList.remove('is-error');el('schema-preview-dialog').showModal();
  },
  init(){
    el('index-mode').onchange=()=>this.updateMode();el('add-index').onclick=()=>this.addIndex();el('apply-indexes').onclick=()=>run(()=>this.apply());
    el('cancel-indexes').onclick=()=>el('schema-index-dialog').close();el('close-indexes-x').onclick=()=>el('schema-index-dialog').close();
    el('close-schema-preview').onclick=()=>el('schema-preview-dialog').close();el('close-schema-preview-x').onclick=()=>el('schema-preview-dialog').close();
  }
};

const auditDeliveryUI={
  timer:null,seq:0,backoffMs:2000,watching:false,visibilityBound:false,
  _ensureVisibility(){
    if(this.visibilityBound)return;
    this.visibilityBound=true;
    document.addEventListener('visibilitychange',()=>{
      if(!document.hidden&&this.watching)this.poll();
    });
  },
  watch(){
    clearTimeout(this.timer);this.seq+=1;this.backoffMs=2000;this.watching=true;
    this._ensureVisibility();
    const target=el('audit-warning');target.hidden=false;target.textContent='日志同步中';
    target.title='数据和审计事件已提交，正在同步日期日志';target.onclick=()=>this.poll();this.timer=setTimeout(()=>this.poll(),250);
  },
  async poll(){
    clearTimeout(this.timer);
    const seq=++this.seq, target=el('audit-warning');
    if(document.hidden)return;
    try{
      const response=await workspaceFetch('/api/manage/audit-delivery');
      const deliveryState=await parseJsonResponse(response,'无法读取日志同步状态');
      if(seq!==this.seq)return;
      this.backoffMs=2000;
      if(!deliveryState.pending_databases.length){this.watching=false;target.hidden=true;return;}
      target.textContent=deliveryState.error?'日志同步待重试':'日志同步中';target.title=deliveryState.error||'数据已保存，日志同步中';
    }catch(error){
      if(seq!==this.seq)return;
      target.textContent='日志同步状态未知';target.title=error.message;
      this.backoffMs=Math.min(30000,this.backoffMs*2);
    }
    if(this.watching&&!document.hidden)this.timer=setTimeout(()=>{if(seq===this.seq&&this.watching)this.poll();},this.backoffMs);
  }
};
