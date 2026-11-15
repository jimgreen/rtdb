// E source rows stay in the File and server staging. Only the session ID is retained locally.
const efileImports={
  key:'rtdb-e-upload-v1',session:null,active:false,paused:false,controller:null,validation:null,
  init(){
    el('efile-pause').onclick=()=>{this.paused=true;this.controller?.abort();this.message('正在暂停…');};
    el('efile-discard').onclick=()=>run(()=>this.discard(),'definition-error');
    el('definition-dialog').addEventListener('cancel',event=>{if(this.active){event.preventDefault();this.paused=true;this.controller?.abort();}});
    el('definition-preview').addEventListener('change',()=>{if(this.session)this.invalidate();});
  },
  message(value,error=false){el('efile-status').textContent=error?'':value;el('definition-error').textContent=error?value:'';},
  remember(){try{if(this.session)localStorage.setItem(this.key,JSON.stringify({id:this.session.id}));else localStorage.removeItem(this.key);}catch{this.message('无法保存续传编号，请勿刷新页面。');}},
  selected(){return /\.e$/i.test(el('definition-file').files[0]?.name||'')||!!this.session&&!el('definition-file').files.length;},
  bound(){return this.session?.database===state.database;},
  invalidate(){this.validation=null;el('apply-definition').disabled=true;this.message('字段或导入方式已变更，请重新校验。');this.controls();},
  optionsChanged(id){
    if(id==='definition-mode'&&this.session?.result){this.invalidate();return;}
    if(id==='definition-file'&&this.session?.result){this.render();this.controls();return;}
    resetDefinitionPreview();this.controls();
  },
  controls(){
    const e=this.selected(),ready=!!this.session?.result,locked=!!this.session?.apply_started;
    el('efile-transfer').hidden=!e;
    el('definition-file').disabled=this.active;el('definition-encoding').disabled=this.active||e&&!!this.session;
    el('definition-mode').disabled=this.active||e&&locked;
    el('efile-pause').disabled=!this.active||backgroundTasks.active;
    el('efile-discard').disabled=this.active||!this.session;
    el('definition-dialog').querySelectorAll('button[value="cancel"]').forEach(b=>b.disabled=this.active);
    if(e){
      el('preview-definition').textContent=ready?'校验字段选择':'上传并解析';
      el('preview-definition').disabled=this.active||locked||ready&&!this.bound();
      el('apply-definition').disabled=this.active||!this.validation||!this.bound();
      el('apply-definition').textContent=locked?'继续 / 核对导入':'确认并生成/更新';
      el('definition-preview').querySelectorAll('select,input').forEach(input=>input.disabled=this.active||locked);
    }else{el('preview-definition').textContent='预览';el('preview-definition').disabled=this.active;el('apply-definition').textContent='确认并生成/更新';}
    el('efile-progress').max=this.session?.size||1;el('efile-progress').value=this.session?.received||0;
    el('efile-target').textContent=`目标：${this.session?.database||state.database} · ${this.session?.filename||'E 文件'}${this.session?' · '+formatBytes(this.session.received)+' / '+formatBytes(this.session.size):''}`;
  },
  render(){
    const result=this.session?.result;if(!result){this.controls();return;}
    const host=el('definition-preview');host.replaceChildren(node('p',`${result.encoding} · ${result.tables.length} 张表；逐表确认后校验全部数据，再执行导入。`));
    const tabs=node('nav',null,'tabs definition-table-tabs');tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','导入表预览');host.append(tabs);
    for(const [index,original] of result.tables.entries()){
      const checked=this.validation?.tables.find(t=>t.name===original.name),item=checked||original;
      const tab=action(item.name,()=>selectDefinitionTable(index),'tab definition-table-tab');tab.id=`definition-table-tab-${index}`;
      tab.setAttribute('role','tab');tab.setAttribute('aria-controls',`definition-table-panel-${index}`);tabs.append(tab);
      tab.onkeydown=event=>{let next;if(event.key==='ArrowRight')next=(index+1)%result.tables.length;else if(event.key==='ArrowLeft')next=(index+result.tables.length-1)%result.tables.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=result.tables.length-1;else return;event.preventDefault();selectDefinitionTable(next,true);};
      const article=node('article',null,'definition-table-panel');article.id=`definition-table-panel-${index}`;article.setAttribute('role','tabpanel');article.setAttribute('aria-labelledby',tab.id);
      article.append(node('strong',`${item.name} · 文件 ${item.incoming} 条${checked?` · ${checked.before} → ${checked.after} 条`:''}`));
      renderEfileDefinitionEditor(article,item);
      if(checked)for(const warning of checked.warnings)article.append(node('p',warning,'import-warning'));
      if(original.sample.length){const details=node('details');details.append(node('summary','源数据样本（前5条、前20列，长文本截取）'));const table=node('table'),head=node('tr');original.source_fields.slice(0,20).forEach(field=>head.append(node('th',field)));table.append(head);
        for(const cells of original.sample){const row=node('tr');cells.forEach(cell=>row.append(node('td',cell)));table.append(row);}details.append(table);article.append(details);}
      host.append(article);
    }
    definitionPreview={format:'e',upload_id:this.session.id};selectDefinitionTable(0);this.controls();
  },
  async restore(){
    if(!this.session){try{this.session=JSON.parse(localStorage.getItem(this.key));}catch{}}
    if(this.session){try{this.session=await request('database-uploads/'+this.session.id);this.validation=this.session.validation||null;el('definition-encoding').value=this.session.encoding;
      if(this.validation)el('definition-mode').value=this.validation.mode;
      this.render();this.message(!this.bound()?'该上传绑定其他数据库，请切换数据库或清除临时上传。':this.session.apply_started?'已恢复导入计划，继续时会跳过已提交的表。':this.session.result?'已恢复解析结果，请核对各表字段。':'已恢复上传进度，重新选择原文件可续传。');
    }catch(error){if([404,410].includes(error.status)){this.session=null;this.validation=null;this.remember();}this.message(error.message);}}
    this.controls();
  },
  async preview(){
    if(this.active)return;
    if(hasPendingData()||state.schemaDirty)throw Error('请先保存或撤销页面修改');
    if(this.session&&!this.bound())throw Error('当前数据库与上传目标不同');
    this.active=true;this.paused=false;this.message('');this.controls();
    try{
      const file=el('definition-file').files[0];
      if(this.session?.result){
        if(file&&!this.sameFile(file))throw Error('请选择原文件，或先清除临时上传');
        const overrides=collectDefinitionOverrides();this.validation=null;
        const result=await request('definition-preview','POST',{upload_id:this.session.id,mode:el('definition-mode').value,definition_overrides:overrides});
        this.validation=result;this.session.validation=result;this.render();this.message('全部数据已校验。请核对记录数、重复KEY警告和各表字段，再确认导入。');return;
      }
      if(!file||!file.size||file.size>128*1024**2||!/\.e$/i.test(file.name))throw Error('请选择不超过128 MiB的非空 E 文件');
      if(!crypto.subtle)throw Error('请通过 localhost 或 HTTPS 访问，以校验上传内容');
      if(this.session&&!this.sameFile(file))throw Error('请选择原文件，或先清除临时上传');
      if(!this.session){this.session={id:crypto.randomUUID().replaceAll('-',''),kind:'e',database:state.database,encoding:el('definition-encoding').value,filename:file.name,size:file.size,last_modified:file.lastModified};this.remember();}
      this.session=await request('database-uploads','POST',this.session);this.session=await request('database-uploads/'+this.session.id);this.controls();
      if(this.session.state==='error')throw Error(this.session.error||'请清除后重新上传');
      const size=this.session.chunk_size;
      for(let i=0;i<this.session.chunks.length;i++){
        if(this.paused)return;
        const {hash}=await databaseUploads.digest(file.slice(i*size,Math.min((i+1)*size,file.size)));
        if(hash!==this.session.chunks[i])throw Error(`第 ${i+1} 块与原文件不同，停止续传`);
      }
      while(this.session.received<file.size){
        if(this.paused)return;
        const offset=this.session.received,{bytes,hash}=await databaseUploads.digest(file.slice(offset,Math.min(offset+size,file.size)));
        for(let attempt=0;attempt<3;attempt++){
          if(this.paused)return;this.controller=new AbortController();
          try{const response=await fetch(`/api/manage/database-uploads/${this.session.id}/chunk?offset=${offset}`,{method:'PUT',headers:{'Content-Type':'application/octet-stream','X-Chunk-SHA256':hash},body:bytes,signal:this.controller.signal});
            const result=await response.json();if(!response.ok){const error=Error(result.error||'上传失败');error.status=response.status;throw error;}this.session=result;break;
          }catch(error){if(this.paused)return;if(attempt===2||error.status&&error.status<500)throw error;await new Promise(resolve=>setTimeout(resolve,300*(attempt+1)));}
        }
        this.controls();this.message('分块上传中，可暂停或刷新后续传。');
      }
      if(this.paused)return;
      await request(`database-uploads/${this.session.id}/finalize`,'POST',{});this.session=await request('database-uploads/'+this.session.id);this.render();
      this.message('解析完成，尚未写入。请逐表确认KEY和类型，然后点击“校验字段选择”。');
    }catch(error){this.message('');throw error;}
    finally{this.active=false;this.controller=null;this.controls();if(this.paused)this.message('已暂停。刷新后重新选择原文件，可继续上传。');}
  },
  sameFile(file){return file.name===this.session.filename&&file.size===this.session.size&&file.lastModified===this.session.last_modified;},
  async apply(){
    if(this.active||!this.validation)throw Error('请先校验字段选择');
    if(!this.bound())throw Error('当前数据库与上传目标不同');
    if(hasPendingData()||state.schemaDirty)throw Error('请先保存或撤销页面修改');
    if(!await confirmAction('确认 E 文件导入',`${this.session.database}：${this.validation.tables.map(t=>`${t.name} ${t.before}→${t.after}条`).join('；')}。逐表提交，失败后可继续未完成步骤。`,this.session.database))return;
    this.active=true;this.session.apply_started=true;this.controls();
    try{
      const result=await request('definition-import','POST',{upload_id:this.session.id,token:this.validation.token});
      let message=result.results.map(t=>`${t.name}：${t.ok?t.replayed?'已提交，跳过重复执行':`成功 ${t.rows} 条（新增${t.inserted}、更新${t.updated}、跳过${t.skipped}、重复${t.duplicates}）`:t.error}`).join('；');
      if(result.pending.length)message+='；待处理：'+result.pending.join('、');
      if(!result.ok)message+='。可点击“继续 / 核对导入”重试失败及待处理表。';
      const warnings=result.results.filter(t=>t.export_warning).map(t=>t.export_warning);if(warnings.length)message+='；'+warnings.join('；');
      this.message(message,!result.ok);notice(message,!result.ok);
      try{await refreshTables();if(state.table)await selectTable(state.table);}catch(error){this.message(message+'；页面刷新失败：'+error.message,!result.ok);}
    }catch(error){this.message('');throw error;}finally{this.active=false;this.controls();}
  },
  async discard(){
    if(this.active||!this.session)return;
    try{await request('database-uploads/'+this.session.id,'DELETE',{});}catch(error){if(![404,410].includes(error.status))throw error;}
    this.session=null;this.validation=null;this.remember();el('definition-file').value='';resetDefinitionPreview();this.controls();this.message('临时上传已清除，已提交数据保留。');
  }
};
document.addEventListener('DOMContentLoaded',()=>efileImports.init());
