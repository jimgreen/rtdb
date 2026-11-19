// CSV data remains in the selected File and server staging, never in localStorage.
const csvImports={
  key:workspaceStorageKey('rtdb-csv-upload-v1'),session:null,target:null,active:false,paused:false,controller:null,
  init(){
    el('csv-start').onclick=()=>this.start();el('csv-pause').onclick=()=>this.pause();
    el('csv-apply').onclick=()=>this.apply();el('csv-discard').onclick=()=>this.discard();
    el('csv-close').onclick=()=>el('csv-dialog').close();
    el('csv-dialog').oncancel=event=>{if(this.active){event.preventDefault();this.pause();}};
    el('csv-file').onchange=()=>{
      const file=el('csv-file').files[0];
      if(file&&this.session?.state==='ready'&&!this.sameFile(file)){this.session=null;this.target={database:state.database,table:state.table};this.remember();this.render();}
      this.controls();
    };
  },
  message(value){el('csv-status').textContent=value;},
  remember(){try{if(this.session)localStorage.setItem(this.key,JSON.stringify({id:this.session.id}));else localStorage.removeItem(this.key);}catch{this.message('浏览器无法保存续传编号，请勿刷新。');}},
  sameFile(file){return this.session&&file.name===this.session.filename&&file.size===this.session.size&&file.lastModified===this.session.last_modified;},
  controls(){
    const ready=this.session?.state==='ready',bound=this.session?.database===state.database&&this.session?.table===state.table;
    el('csv-start').disabled=this.active||!el('csv-file').files.length;
    el('csv-start').textContent=this.session&&!ready?'继续上传并预览':'上传并预览';
    el('csv-pause').disabled=!this.active||state.busy;el('csv-file').disabled=this.active;
    el('csv-encoding').disabled=this.active||!!this.session;
    el('csv-mode').disabled=this.active;el('csv-close').disabled=this.active;
    el('csv-discard').disabled=this.active||!this.session;
    el('csv-apply').disabled=this.active||!ready||!bound;
  },
  render(){
    const value=this.session,target=value||this.target;
    el('csv-target').textContent=`目标：${target?.database||'未选择'} / ${target?.table||'未选择'}`;
    el('csv-progress').max=value?.size||1;el('csv-progress').value=value?.received||0;
    el('csv-detail').textContent=value?`${value.filename||''} · 已确认 ${formatBytes(value.received)} / ${formatBytes(value.size)}`:'请选择 CSV 文件';
    const host=el('csv-preview');host.replaceChildren();
    if(value?.result){
      const result=value.result;
      host.append(node('p',`校验通过：${result.rows} 条记录 · ${result.encoding}。显示前20条，长文本仅显示前160字符。`));
      const table=node('table'),thead=node('thead'),head=node('tr'),body=node('tbody');
      for(const column of result.columns)head.append(node('th',`${column.name} · ${column.type.toUpperCase()}${column.key?' KEY':''}`));
      thead.append(head);table.append(thead,body);
      for(const row of result.sample){const tr=node('tr');for(const column of result.columns)tr.append(node('td',row[column.name]??'NULL'));body.append(tr);}
      host.append(table);
    }
    this.controls();
  },
  async open(){
    requireTable();
    if(hasPendingData()||state.schemaDirty)throw Error('请先保存或撤销页面修改，再导入 CSV');
    this.target={database:state.database,table:state.table};
    if(!this.session){try{this.session=JSON.parse(localStorage.getItem(this.key));}catch{}}
    if(this.session){
      try{this.session=await request('database-uploads/'+this.session.id);el('csv-encoding').value=this.session.encoding;
        this.message(this.session.database!==state.database||this.session.table!==state.table?'暂存文件绑定其他数据库或表，请切换到目标表，或清除临时上传。':this.session.state==='ready'?'校验结果已恢复，请选择方式并确认导入。':'发现未完成上传，请重新选择原文件后继续。');}
      catch(error){if([404,410].includes(error.status)){this.session=null;this.remember();}this.message(error.message);}
    }else this.message('上传和预览不修改目标表，确认后才执行导入。');
    this.render();el('csv-dialog').showModal();
  },
  pause(){this.paused=true;this.controller?.abort();this.message('正在暂停…');},
  async start(){
    if(this.active||state.busy)return;
    const file=el('csv-file').files[0];if(!file)return;
    if(!crypto.subtle){this.message('请通过 localhost 或 HTTPS 访问，以便校验上传内容。');return;}
    if(!/\.csv$/i.test(file.name)||!file.size||file.size>128*1024**2){this.message('请选择不超过128 MiB的非空 CSV 文件');return;}
    if(this.session&&!this.sameFile(file)){this.message('请选择原文件，或先清除临时上传。');return;}
    if(hasPendingData()||state.schemaDirty){this.message('请先保存或撤销页面修改');return;}
    this.active=true;this.paused=false;this.controls();
    try{
      if(!this.session){this.session={id:crypto.randomUUID().replaceAll('-',''),kind:'csv',...this.target,filename:file.name,size:file.size,last_modified:file.lastModified,encoding:el('csv-encoding').value};this.remember();}
      // After a lost create response only the ID may be in browser storage; open() resolves it.
      this.session=await request('database-uploads','POST',this.session);
      this.session=await request('database-uploads/'+this.session.id);this.remember();this.render();
      if(this.session.state==='ready'){this.message('文件已校验，可以确认导入。');return;}
      if(this.session.state==='error')throw Error(this.session.error||'请清除临时上传后重试');
      const size=this.session.chunk_size;
      for(let i=0;i<this.session.chunks.length;i++){
        if(this.paused)return;
        const {hash}=await databaseUploads.digest(file.slice(i*size,Math.min((i+1)*size,file.size)));
        if(hash!==this.session.chunks[i])throw Error(`第 ${i+1} 块与原文件不同，已停止续传`);
        this.message(`核对已上传内容 ${i+1} / ${this.session.chunks.length}`);
      }
      const started=performance.now(),initial=this.session.received;
      while(this.session.received<file.size){
        if(this.paused)return;
        const offset=this.session.received,{bytes,hash}=await databaseUploads.digest(file.slice(offset,Math.min(offset+size,file.size)));
        for(let attempt=0;attempt<3;attempt++){
          if(this.paused)return;this.controller=new AbortController();
          try{
            const response=await workspaceFetch(`/api/manage/database-uploads/${this.session.id}/chunk?offset=${offset}`,{method:'PUT',headers:{'Content-Type':'application/octet-stream','X-Chunk-SHA256':hash},body:bytes,signal:this.controller.signal});
            const result=await response.json();if(!response.ok){const error=Error(result.error||'上传失败');error.status=response.status;throw error;}
            this.session=result;break;
          }catch(error){if(this.paused)return;if(attempt===2||error.status&&error.status<500)throw error;await new Promise(resolve=>setTimeout(resolve,300*(attempt+1)));}
        }
        this.render();this.message(`上传速度 ${formatBytes((this.session.received-initial)/Math.max(.001,(performance.now()-started)/1000))}/s`);
      }
      if(this.paused)return;
      await run(async()=>{await request(`database-uploads/${this.session.id}/finalize`,'POST',{});this.session=await request('database-uploads/'+this.session.id);this.render();this.message('校验通过。请核对样本、目标表及导入方式后确认。');},'csv-status');
    }catch(error){this.message(error.message);}
    finally{this.active=false;this.controller=null;this.controls();if(this.paused)this.message('已暂停，已确认的内容保留。刷新后重新选择原文件可续传。');}
  },
  async apply(){
    if(this.active||state.busy||this.session?.state!=='ready')return;
    if(hasPendingData()||state.schemaDirty){this.message('请先保存或撤销页面修改');return;}
    if(this.session.database!==state.database||this.session.table!==state.table){this.message('当前数据库或表与上传目标不同');return;}
    const mode=el('csv-mode').value,target=this.session;
    this.active=true;this.controls();
    try{await run(async()=>{
      if(mode!=='append'&&!await confirmAction('确认 CSV 导入',`${mode==='replace'?'替换整表':'按主键合并'} ${target.database} / ${target.table}，共 ${target.result.rows} 条`,target.table))return;
      const result=await request(tablePath('import',target.table),'POST',{upload_id:target.id,token:target.result.token,mode,confirm:target.table});
      const success=result.replayed?'该文件已经导入，本次未重复写入。':`导入成功：${result.imported} 条，${mode==='append'?'追加':mode==='merge'?'合并':'替换'}。`;
      this.message(success);
      try{await refreshTables();await queryRows();}catch(error){this.message(success+' 页面刷新失败：'+error.message);}
    },'csv-status');}finally{this.active=false;this.controls();}
  },
  async discard(){
    if(this.active||state.busy||!this.session)return;
    await run(async()=>{try{await request('database-uploads/'+this.session.id,'DELETE',{});}catch(error){if(![404,410].includes(error.status))throw error;}
      this.session=null;this.target={database:state.database,table:state.table};this.remember();this.render();this.message('已清除临时上传，已导入的数据保留。');},'csv-status');
  }
};
document.addEventListener('DOMContentLoaded',()=>csvImports.init());
