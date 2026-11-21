const receiptMaintenance={
  database:null,timer:null,seq:0,
  async open(){
    this.database=state.database;if(!this.database)throw Error('请先选择数据库');
    el('receipt-database').textContent=this.database;
    el('receipt-close').onclick=()=>el('receipt-maintenance-dialog').close();
    el('receipt-close-x').onclick=()=>el('receipt-maintenance-dialog').close();
    el('receipt-maintenance-dialog').onclose=()=>{clearTimeout(this.timer);this.seq+=1;};
    bindPresetControl(el('receipt-days-preset'),el('receipt-days'));
    el('receipt-config').onclick=()=>run(async()=>{
      const days=Number(el('receipt-days').value);
      await request('receipt-maintenance','PUT',{days});notice('回执压缩配置已保存');await this.refresh();
    });
    el('receipt-archive').onclick=()=>run(async()=>{
      await request('receipt-maintenance','POST',{});notice('已申请按配置压缩历史回执');await this.refresh();
    });
    await this.refresh(true);el('receipt-maintenance-dialog').showModal();
  },
  async refresh(initial=false){
    clearTimeout(this.timer);
    const seq=++this.seq, targetDb=this.database;
    const response=await workspaceFetch('/api/manage/receipt-maintenance?database='+encodeURIComponent(targetDb));
    const result=await parseJsonResponse(response,'读取维护状态失败');
    if(seq!==this.seq||this.database!==targetDb||(!initial&&!el('receipt-maintenance-dialog').open))return;
    if(initial)syncPresetControl(el('receipt-days-preset'),el('receipt-days'),result.days);
    const list=el('receipt-statistics');list.replaceChildren();
    for(const [label,value] of [['等待日志同步',result.pending+' 条'],['已同步回执',result.delivered+' 条'],
      ['已压缩回执',result.archived+' 条'],['归档明细大小',formatBytes(result.original_bytes)+' → '+formatBytes(result.compressed_bytes)],
      ['最早待同步时间',result.oldest_pending||'无']])list.append(node('dt',label),node('dd',value));
    const delivery=result.delivery||{},archiving=delivery.archive_databases?.includes(targetDb);
    const error=delivery.archive_errors?.[targetDb];
    el('receipt-status').textContent=error||(archiving?'正在分批压缩…':result.pending?'日志正在同步，失败后自动重试':'当前没有待同步日志');
    if(delivery.error&&result.pending)el('receipt-status').textContent=delivery.error;
    this.timer=setTimeout(()=>{if(seq===this.seq&&el('receipt-maintenance-dialog').open)this.refresh().catch(error=>{if(seq===this.seq)el('receipt-status').textContent=error.message;});},2000);
  }
};
