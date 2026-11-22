// Browser-local recovery only. Restoring a draft never writes database records.
const localDrafts={
  prefix:'rtdb-cell-draft-v2:',owner:crypto.randomUUID(),owned:new Set(),timer:null,suspended:false,signatures:new Map(),peers:new Map(),ready:false,
  async init(){
    try{await draftStore.init();this.ready=true;}catch(error){notice('本地草稿存储不可用：'+error.message,true);return;}
    if(typeof BroadcastChannel!=='undefined'){
      this.channel=new BroadcastChannel('rtdb-draft-presence');
      this.channel.onmessage=async({data})=>{
        if(data.owner===this.owner)return;
        if(data.type==='hello')this.announce('alive');
        if(data.type==='bye')this.peers.delete(data.owner);else this.peers.set(data.owner,Date.now());
        if(data.type==='changed')try{await draftStore.queue.catch(()=>{});await draftStore.refresh();}catch(error){notice('本地草稿刷新失败：'+error.message,true);}
      };
      this.announce('hello');this.heartbeat=setInterval(()=>this.announce('alive'),10000);
    }
  },
  announce(type){this.channel?.postMessage({owner:this.owner,type});},
  activePeer(owner){const at=this.peers.get(owner);if(at&&Date.now()-at<25000)return true;this.peers.delete(owner);return false;},
  key(kind='data'){return state.database&&(state.table||kind==='schema')?this.prefix+JSON.stringify([state.historyScope,state.database,state.table||'__new__',this.owner,kind]):null;},
  candidates(kind=null){return [...draftStore.cache.values()].filter(value=>value.scope===state.historyScope&&value.database===state.database&&value.table===state.table&&(!kind||value.kind===kind)).sort((a,b)=>b.at.localeCompare(a.at));},
  read(key){return key?draftStore.cache.get(key):draftStore.cache.get(this.key())||this.candidates('data')[0];},
  async dropKey(key,revision=null){
    if(!key)return;
    try{await draftStore.remove(key,revision);localStorage.removeItem('rtdb-draft-emergency:'+key);this.owned.delete(key);this.signatures.delete(key);this.announce('changed');}
    catch(error){notice('草稿清理失败，数据库保存结果不受影响：'+error.message,true);}
  },
  schedule(){
    if(this.suspended||!this.ready)return;
    clearTimeout(this.timer);let save=false;
    for(const kind of ['data','schema']){
      const key=this.key(kind),pending=kind==='schema'?state.schemaDirty:hasPendingData();if(!key)continue;
      if(pending)save=true;
      else if(this.owned.has(key))void this.dropKey(key,this.read(key)?.revision).catch(error=>notice('草稿清理失败：'+error.message,true));
    }
    if(save)this.timer=setTimeout(()=>this.flush(),250);
  },
  capture(kind){
      const key=this.key(kind);if(!key||(kind==='schema'?!state.schemaDirty:!hasPendingData()))return null;
      if(kind==='schema')return {rawSchema:{indexes:state.schemaIndexes,name:el('schema-name').value,label:el('schema-label').value,rows:[...el('schema-fields').querySelectorAll('tbody tr')].map(row=>({original:row.dataset.originalName,fields:Object.fromEntries([...row.querySelectorAll('[data-property]')].map(input=>[input.dataset.property,input.type==='checkbox'?input.checked:input.value]))}))}};
      const edits=new Map();
      for(const edit of state.pendingEdits.values()){
        const identity=recordKey(edit.record);if(!edits.has(identity))edits.set(identity,[]);
        edits.get(identity).push({field:edit.column.eng_name,raw:edit.raw});
      }
      return {layout:state.records.map(record=>({ref:record.ref,draftId:record.draftId,
        ...(record.draftId||edits.has(recordKey(record))?{baseline:record.baseline,edits:edits.get(recordKey(record))||[]}:{} )})),
        query:state.lastQuery,offset:state.offset,pageSize:state.pageSize};
  },
  async flush(emergency=false){
    clearTimeout(this.timer);if(this.suspended||!this.ready)return false;
    try{
      const writes=[];
      for(const kind of ['data','schema']){
        const value=this.capture(kind),key=this.key(kind);if(!value)continue;
        const signature=JSON.stringify([state.version,value]);
        let data=this.read(key);
        if(this.signatures.get(key)!==signature){
          data={...value,key,scope:state.historyScope,owner:this.owner,kind,revision:crypto.randomUUID(),at:new Date().toISOString(),database:state.database,table:state.table,schema:state.version};
          const pending=draftStore.put(data).then(()=>this.signatures.set(key,signature));writes.push(pending);this.owned.add(key);
        }
        if(emergency&&data){const encoded=JSON.stringify(data);if(encoded.length<=2*1024*1024)localStorage.setItem('rtdb-draft-emergency:'+key,encoded);}
      }
      if(writes.length){await Promise.all(writes);this.announce('changed');}return true;
    }catch(error){notice('本地草稿未能保存：'+error.message+'；请勿关闭页面',true);return false;}
  },
  async saving(kind='data'){await this.flush();const key=this.key(kind),saved=this.read(key);return saved?{draftKey:key,draftRevision:saved.revision}:{};},
  async offer(manual=false){
    if(!this.ready||hasPendingData()||state.schemaDirty)return;
    await draftStore.queue.catch(()=>{});await draftStore.refresh();
    const entries=this.candidates().filter(value=>manual||!this.activePeer(value.owner));
    if(!entries.length)return;
    const select=el('draft-choice');select.replaceChildren(...entries.map((saved,index)=>new Option(`${saved.kind==='schema'?'表结构':'记录'} · ${new Date(saved.at).toLocaleString()} · ${saved.owner===this.owner?'本页面':this.activePeer(saved.owner)?'其他活动页面':'历史页面'}`,String(index))));
    const show=saved=>{
    const compatible=saved.schema===state.version;
    el('draft-recovery-caption').textContent=`${saved.database} / ${saved.table||'新建表'} · ${saved.kind==='schema'?'未保存的表结构':saved.layout.filter(r=>r.draftId||r.edits?.length).length+' 条记录'}。`;
    el('draft-recovery-warning').textContent=compatible?'恢复后先核对服务器版本；本操作不会写入数据库。':'表结构已变化，不能直接恢复。可导出草稿后核对，或删除草稿。';
    el('restore-local-draft').disabled=!compatible;
    el('restore-local-draft').onclick=()=>{el('draft-recovery-dialog').close();run(()=>this.restore(saved));};
    el('export-local-draft').onclick=()=>download(JSON.stringify(saved,null,2),`${saved.table||'new-table'}-draft.json`,'application/json');
    el('delete-local-draft').disabled=this.activePeer(saved.owner);
    el('delete-local-draft').onclick=()=>run(async()=>{await this.dropKey(saved.key,saved.revision);el('draft-recovery-dialog').close();notice('所选历史草稿已删除');});
    };select.onchange=()=>show(entries[Number(select.value)]);show(entries[0]);
    el('draft-recovery-dialog').showModal();
  },
  async restore(saved){
    if(saved.schema!==state.version||saved.database!==state.database||saved.table!==state.table)throw Error('草稿与当前数据库或结构不匹配');
    if(saved.kind==='schema'){
      const raw=saved.rawSchema;
      renderSchema({indexes:raw.indexes,head:{eng_name:raw.name,chn_name:raw.label},columns:raw.rows.map(row=>({...row.fields,__original_name:row.original,default_value:undefined}))});
      el('schema-name').value=raw.name;el('schema-label').value=raw.label;
      const baselineByName=new Map((state.definition?.columns||[]).map(col=>[col.eng_name,col]));
      [...el('schema-fields').querySelectorAll('tbody tr')].forEach((row,index)=>row.querySelectorAll('[data-property]').forEach(input=>{
        const prop=input.dataset.property, value=raw.rows[index].fields[prop];
        const baseline=baselineByName.get(raw.rows[index].original);
        if(baseline){
          const baseVal=prop==='primary_key'?String(Boolean(baseline.primary_key)):prop==='chn_name'?chineseName(baseline):prop==='default_value'?(baseline.default_value==null?'':typeof baseline.default_value==='string'&&!integerTypes.has(columnKind(baseline))?JSON.stringify(baseline.default_value):cellText(baseline.default_value)):String(baseline[prop]??'');
          input.dataset.originalValue=baseVal;
        }
        if(input.type==='checkbox')input.checked=value;else input.value=value;
        const current=input.type==='checkbox'?String(input.checked):String(input.value);
        input.closest('td')?.classList.toggle('schema-modified', !baseline || current!==input.dataset.originalValue);
      }));
      state.schemaDirty=true;tab('schema');const persisted=await this.flush();
      if(!persisted)return;
      if(!this.activePeer(saved.owner)&&saved.key!==this.key('schema'))await this.dropKey(saved.key,saved.revision);
      notice('已恢复结构草稿，尚未写入数据库；保存时仍检查结构版本和变更限制');return;
    }
    const originals=saved.layout.filter(r=>!r.draftId);
    const result=await request(tablePath('resolve-records'),'POST',{keys:originals.map(r=>r.ref.key),schema_version:saved.schema});
    const conflicts=[];let index=0;
    this.suspended=true;
    try{
      state.pendingEdits.clear();state.records=[];
      for(const item of saved.layout){
        const current=item.draftId?null:result.records[index++];let record;
        if(item.draftId){record={draftId:++state.draftSequence,baseline:structuredClone(item.baseline),row:structuredClone(item.baseline)};}
        else if(item.edits?.length){
          record={ref:item.ref,baseline:structuredClone(item.baseline),row:structuredClone(item.baseline)};
          if(!current||current.ref.version!==item.ref.version)conflicts.push({key:item.ref.key,fields:item.edits.map(e=>e.field),current:current?.row||null,current_ref:current?.ref||null});
        }else if(current){record={...current,baseline:structuredClone(current.row)};}else continue;
        state.records.push(record);
        for(const edit of item.edits||[]){const column=state.definition.columns.find(c=>c.eng_name===edit.field);if(!column)throw Error('草稿字段已不存在');setPendingCell(record,column,edit.raw,false);}
      }
      const validSizes=[...el('page-size').options].map(option=>Number(option.value));
      const restoredPageSize=validSizes.includes(Number(saved.pageSize))?Number(saved.pageSize):(Number(el('page-size').value)||100);
      state.offset=saved.offset;state.lastQuery=saved.query;state.pageSize=restoredPageSize;el('page-size').value=String(restoredPageSize);
      el('sql-editor').value=saved.query?.sql||defaultSql();state.queryVersion=null;
      resetEditHistory();clearGridSelection();clearAddedChartSeries();renderGrid();drawChart();
    }finally{this.suspended=false;}
    this.owned.add(this.key());updateEditActions();const persisted=await this.flush();
    if(persisted&&!this.activePeer(saved.owner)&&saved.key!==this.key())await this.dropKey(saved.key,saved.revision);
    if(persisted)notice('已恢复草稿工作页，请检查后 Ctrl+S 保存；保存后按最新数据刷新');
    if(conflicts.length){const error=Error('草稿中的记录已变化，请比较服务器值与本地值');error.details={conflicts};showSaveWarning(error);}
  }
};
