// Keep only task identities across reloads, never operation bodies or file contents.
const backgroundTasks = {
  storageKey: workspaceStorageKey('rtdb-active-task-v1'), active: false,
  pending() { try{return !!sessionStorage.getItem(this.storageKey);}catch{return this.active;} },
  supports(path, method, body) {
    const route=path.split('?')[0];
    if(method==='POST' && route==='storage-maintenance')return true;
    if(method==='POST' && /^database-snapshots(?:\/[a-f0-9]{32}\/restore)?$/.test(route))return true;
    if(method==='POST' && /^database-uploads\/[a-f0-9]{32}\/finalize$/.test(route))return true;
    return method==='GET' && route==='logs' || method==='PUT' && /^tables\/[^/]+\/(schema|cell-edits|rows)$/.test(route) || method==='DELETE'&&/^tables\/[^/]+\/(rows|schema)$/.test(route) ||
      method==='POST' && (/^(definition-preview|definition-import|backup|restore-preview|restore|database-files\/rebuild-from-embedded)$/.test(route) || /^tables\/[^/]+\/(sql-export|import|rows|clear)$/.test(route) || /^tables\/[^/]+\/sql$/.test(route)&&!body?.readonly_only&&/^\s*(INSERT|UPDATE|DELETE)\b/i.test(body?.sql||''));
  },
  remember(task) { try { sessionStorage.setItem(this.storageKey,JSON.stringify({id:task.id,database:task.database,path:task.path,draftKey:task.draftKey,draftRevision:task.draftRevision})); } catch {} },
  clear() { try { sessionStorage.removeItem(this.storageKey); } catch {} },
  async prepare(path,database) {
    const task={id:crypto.randomUUID().replaceAll('-',''),database,path:path.split('?')[0],...(/\/(cell-edits|schema)$/.test(path)?await localDrafts.saving(path.endsWith('/schema')?'schema':'data'):{})};
    this.remember(task);return task;
  },
  async follow(task) {
    if(this.active)throw new Error('已有后台任务正在跟踪，请等待完成');
    this.active=true;this.remember(task);
    const dialog=document.getElementById('task-dialog'), phase=document.getElementById('task-phase'),
      bar=document.getElementById('task-progress'), cancel=document.getElementById('task-cancel'),
      warning=document.getElementById('task-warning'), completed=document.getElementById('task-completed');
    const url=`/api/manage/tasks/${encodeURIComponent(task.id)}${task.database?'?database='+encodeURIComponent(task.database):''}`;
    const endpoint=action=>action ? url.replace(task.id,task.id+'/'+action) : url;
    const started=performance.now();let current=task, retries=0;
    if(!document.getElementById('task-browse')){
      const link=node('a','另页浏览数据库','secondary');link.id='task-browse';link.href='/';link.target='_blank';link.rel='noopener noreferrer';
      dialog.querySelector('.dialog-actions').prepend(link);
    }
    const uploadTask=task.path.startsWith('database-uploads/');
    document.getElementById('task-caption').textContent=`${task.database||(uploadTask?'文件上传':task.path==='logs'?'服务日志':'数据库管理')} · ${uploadTask?'校验上传文件':({'definition-preview':'导入预览','definition-import':'导入定义与数据',backup:'备份',restore:'恢复',logs:'查询日志'}[task.path]||'执行操作')}`;
    warning.textContent='';dialog.oncancel=e=>e.preventDefault();dialog.showModal();
    cancel.onclick=async()=>{
      cancel.disabled=true;
      try {
        const response=await workspaceFetch(endpoint('cancel'),{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}), data=await response.json();
        if(!response.ok)throw Error(data.error||'取消请求失败');
        current=data;warning.textContent='已申请取消，正在等待安全边界；最终结果以服务端状态为准。';
      } catch(error) { warning.textContent=error.message; }
    };
    try {
      while(true) {
        phase.textContent=`${current.phase||'恢复任务进度'} · 已跟踪 ${((performance.now()-started)/1000).toFixed(1)} 秒`;
        if(current.total>0){bar.max=current.total;bar.value=current.done;}else bar.removeAttribute('value');
        completed.textContent=current.completed_tables?.length?`已提交：${current.completed_tables.join('、')}`:'';
        cancel.disabled=!current.cancellable||current.cancel_requested||!['running','queued'].includes(current.status);
        if(['done','failed','cancelled','interrupted'].includes(current.status)) {
          this.clear();
          if(current.result_expired)throw Error('任务已执行完成，详细结果已过保留期；请查看日志，勿重复提交写入');
          if(current.status==='done'){this.lastTimings=current.timings||{};if(task.draftKey)await localDrafts.dropKey(task.draftKey,task.draftRevision);reportAuditWarning(current.result);return current.result;}
          const error=Error(current.error||current.phase);error.details=current.details;error.status=current.error_status;
          if(current.completed_tables?.length)error.message+=`；已提交：${current.completed_tables.join('、')}`;
          throw error;
        }
        try {
          const target=endpoint();
          const response=await workspaceFetch(target+(target.includes('?')?'&':'?')+'wait=2&revision='+Number(current.revision||0)), data=await response.json();
          if(!response.ok){const error=Error(data.error||'任务查询失败');error.status=response.status;throw error;}
          current=data;retries=0;
        } catch(error) {
          if(error.status===404){this.clear();throw error;}
          warning.textContent='连接暂时中断，正在恢复查询；任务可能仍在执行，请勿重复提交。';
          if(++retries>=10)throw Error('无法取得任务状态。任务可能仍在执行；刷新页面将恢复查询，请勿重复提交操作。');
          await new Promise(resolve=>setTimeout(resolve,Math.min(2000,250*retries)));
        }
      }
    } finally {cancel.onclick=null;dialog.close();this.active=false;}
  },
  async resume() {
    let task;try {task=JSON.parse(sessionStorage.getItem(this.storageKey));} catch {this.clear();}
    if(!task?.id)return;
    const result=await this.follow(task);
    if(result.text!==undefined && result.filename)download(result.text,result.filename,result.mime||'text/plain');
    notice(`后台任务已完成${task.database?'：'+task.database:''}。${result.ok===false?'部分项目失败，请查看日志。':'可选择数据库查看最新结果。'}`,result.ok===false);
  },
  async history() {
    const response=await workspaceFetch('/api/manage/tasks'), data=await response.json();
    if(!response.ok)throw Error(data.error||'任务列表加载失败');
    const host=el('task-history-grid');host.replaceChildren();
    const table=node('table'), head=node('tr');
    ['时间','数据库','操作','状态 / 结果','排队 / 执行 ms',''].forEach(text=>head.append(node('th',text)));
    table.append(head);
    for(const task of data.tasks) {
      const row=node('tr');
      [new Date(task.created*1000).toLocaleString(),task.database||'服务日志',task.path,task.error||task.phase,
        (task.timings?.queue_ms??'—')+' / '+(task.timings?.execute_ms??'—')].forEach(text=>row.append(node('td',text)));
      const td=node('td'), button=node('button','查看','secondary small');button.type='button';
      button.onclick=()=>{el('task-history-dialog').close();run(async()=>{const result=await this.follow({id:task.id,database:task.database,path:task.path});
        if(result.text!==undefined&&result.filename)download(result.text,result.filename,result.mime||'text/plain');
        else notice(`任务完成：${JSON.stringify(result).slice(0,500)}`);
      });};
      td.append(button);row.append(td);table.append(row);
    }
    host.append(table);el('task-history-dialog').showModal();
  }
};
