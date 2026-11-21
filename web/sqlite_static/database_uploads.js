// A local file is read one slice at a time. Only the resumable identity is persisted.
const databaseUploads={
  get key() { return workspaceStorageKey('rtdb-snapshot-upload-v1'); },session:null,active:false,paused:false,controller:null,
  init(){
    el('open-chunk-upload').onclick=()=>run(()=>this.open());
    el('chunk-start').onclick=()=>this.start();
    el('chunk-pause').onclick=()=>this.pause();
    el('chunk-discard').onclick=()=>this.discard();
    el('chunk-restore').onclick=()=>this.restore();
    el('chunk-close').onclick=()=>el('chunk-upload-dialog').close();
    el('chunk-close-x').onclick=()=>el('chunk-upload-dialog').close();
    el('chunk-upload-dialog').addEventListener('cancel',event=>{if(this.active){event.preventDefault();this.pause();}});
    el('chunk-file').onchange=()=>{
      const file=el('chunk-file').files[0];
      if(file&&this.session?.state==='ready'&&!this.sameFile(file)){
        this.session=null;this.remember();el('chunk-sha').value='';el('chunk-target').value='';el('chunk-label').value='';this.render();
      }
      this.controls();
    };
  },
  remember(){
    try{if(this.session)localStorage.setItem(this.key,JSON.stringify({id:this.session.id,filename:this.session.filename,size:this.session.size,last_modified:this.session.last_modified,sha256:this.session.sha256}));else localStorage.removeItem(this.key);}
    catch{this.message('浏览器无法保存续传编号，请勿刷新页面。');}
  },
  message(text){el('chunk-status').textContent=text;},
  controls(){
    const ready=this.session?.state==='ready';
    el('chunk-start').disabled=this.active||!el('chunk-file').files.length;
    el('chunk-start').textContent=this.session&&!ready?'继续上传并校验':'上传并校验';
    el('chunk-pause').disabled=!this.active||state.busy;
    el('chunk-discard').disabled=this.active||!this.session;
    el('chunk-file').disabled=this.active;el('chunk-sha').disabled=this.active||!!this.session;
    el('chunk-close').disabled=this.active;
    el('chunk-restore').disabled=this.active||!ready;
    el('chunk-target').disabled=this.active||!ready;el('chunk-label').disabled=this.active||!ready;
  },
  render(){
    const value=this.session,bar=el('chunk-progress');bar.max=value?.size||1;bar.value=value?.received||0;
    el('chunk-session').textContent=value?`${value.filename} · 已确认 ${formatBytes(value.received||0)} / ${formatBytes(value.size)} · 临时上传保留至 ${new Date(value.expires_at*1000).toLocaleString()}`:'请选择本地整库快照';
    el('chunk-preview').textContent=value?.result?`校验通过：${value.result.table_count} 张表（${value.result.tables.join('、')}）\nSHA-256：${value.result.sha256}`:'';
    if(value?.result&&!el('chunk-target').value)el('chunk-target').value=value.result.database.replace(/\.sqlite3$/,'')+'_copy';
    this.controls();
  },
  async open(){
    if(!this.session){try{this.session=JSON.parse(localStorage.getItem(this.key));}catch{}}
    if(this.session){
      try{this.session=await request('database-uploads/'+this.session.id);el('chunk-sha').value=this.session.sha256||'';
        this.message(this.session.state==='ready'?'上传已完成，可恢复为新数据库。':'发现未完成上传，请重新选择原文件后继续。');}
      catch(error){if(error.status===404||error.status===410){this.session=null;this.remember();}this.message(error.message);}
    }else this.message('每块校验后确认接收；暂停或刷新不会清除已确认的内容。');
    this.render();el('chunk-upload-dialog').showModal();
  },
  pause(){this.paused=true;this.controller?.abort();this.message('正在暂停，将保留服务端已确认的进度…');},
  async digest(blob){
    const bytes=await blob.arrayBuffer();
    const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(value=>value.toString(16).padStart(2,'0')).join('');
    return {bytes,hash};
  },
  sameFile(file){return this.session&&file.name===this.session.filename&&file.size===this.session.size&&file.lastModified===this.session.last_modified;},
  async start(){
    if(this.active||state.busy)return;
    const file=el('chunk-file').files[0];if(!file)return;
    if(!crypto.subtle){this.message('此入口需要通过 localhost 或 HTTPS 访问，以便校验上传内容。');return;}
    if(!/\.sqlite3$/i.test(file.name)||!file.size||file.size>2*1024**3){this.message('请选择不超过2 GiB的 .sqlite3 整库快照');return;}
    if(this.session&&!this.sameFile(file)&&this.session.state!=='ready'){this.message('文件名称、大小或修改时间不同。请选择原文件，或先放弃当前临时上传。');return;}
    this.active=true;this.paused=false;this.controls();
    try{
      if(!this.sameFile(file)){
        this.session={id:crypto.randomUUID().replaceAll('-',''),filename:file.name,size:file.size,last_modified:file.lastModified,sha256:el('chunk-sha').value.trim()};this.remember();
      }
      this.session=await request('database-uploads','POST',this.session);this.remember();this.render();
      if(this.session.state==='ready'){this.message('该文件已校验完成，可直接恢复。');return;}
      if(this.session.state==='error')throw Error(this.session.error||'上传校验失败，请放弃后重新上传');
      const size=this.session.chunk_size;
      // Verify every acknowledged slice, not just file name/size or the first slice.
      for(let index=0;index<this.session.chunks.length;index++){
        if(this.paused)return;
        const offset=index*size,{hash}=await this.digest(file.slice(offset,Math.min(offset+size,file.size)));
        if(hash!==this.session.chunks[index])throw Error(`本地文件第 ${index+1} 块内容与已上传内容不同，已停止续传。`);
        this.message(`核对本地续传文件：${index+1} / ${this.session.chunks.length} 块`);
      }
      const started=performance.now(),initial=this.session.received;
      while(this.session.received<file.size){
        if(this.paused)return;
        const offset=this.session.received,{bytes,hash}=await this.digest(file.slice(offset,Math.min(offset+size,file.size)));
        if(this.paused)return;
        let answer;
        for(let attempt=0;attempt<3;attempt++){
          if(this.paused)return;this.controller=new AbortController();
          try{
            const response=await workspaceFetch(`/api/manage/database-uploads/${this.session.id}/chunk?offset=${offset}`,{method:'PUT',headers:{'Content-Type':'application/octet-stream','X-Chunk-SHA256':hash},body:bytes,signal:this.controller.signal});
            const result=await parseJsonResponse(response,'上传分块失败');
            answer=result;break;
          }catch(error){if(this.paused)return;if(attempt===2||error.status&&error.status<500)throw error;await new Promise(resolve=>setTimeout(resolve,300*(attempt+1)));}
        }
        this.session=answer;this.render();
        const speed=(this.session.received-initial)/Math.max(.001,(performance.now()-started)/1000);
        this.message(`上传已确认 ${formatBytes(this.session.received)} / ${formatBytes(file.size)} · ${formatBytes(speed)}/s`);
      }
      if(this.paused)return;
      this.message('上传完成，正在检查整库内容…');
      await run(async()=>{
        const result=await request(`database-uploads/${this.session.id}/finalize`,'POST',{});
        this.session=await request('database-uploads/'+this.session.id);this.session.result=result;
        this.message('校验通过。请确认下方数据库名称，恢复为新库。');this.render();
      },'chunk-status');
    }catch(error){this.message(error.message);}
    finally{this.active=false;this.controller=null;this.controls();if(this.paused)this.message('已暂停。点击“继续上传并校验”可续传；刷新后需重新选择同一文件。');}
  },
  async discard(){
    if(this.active||!this.session||state.busy)return;
    await run(async()=>{
      try{await request('database-uploads/'+this.session.id,'DELETE',{});}catch(error){if(error.status!==404&&error.status!==410)throw error;}
      this.session=null;this.remember();el('chunk-sha').value='';this.render();this.message('临时上传已放弃；已生成的快照和数据库保持不变。');
    },'chunk-status');
  },
  async restore(){
    if(this.active||state.busy||!this.session?.result)return;
    const name=el('chunk-target').value.trim();if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)){this.message('数据库英文名必须为英文标识符');return;}
    this.active=true;this.controls();
    try{await run(async()=>{
      const item=this.session.result;
      const result=await request(`database-snapshots/${item.id}/restore?database=${encodeURIComponent(item.database)}`,'POST',{name,chn_name:el('chunk-label').value.trim()});
      await refreshTables();this.message(`已恢复为 ${result.filename}，可从左侧数据库列表选择。${result.definition_warning||''}`);
    },'chunk-status');}finally{this.active=false;this.controls();}
  }
};
document.addEventListener('DOMContentLoaded',()=>databaseUploads.init());
