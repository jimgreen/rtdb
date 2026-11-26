// Real upload/resume/restore interactions, restricted to workflow-lab.
async(page)=>{
  const origin=new URL(page.url()).origin,checks=[],errors=[],offsets=[];
  const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
  const idle=()=>page.waitForFunction(()=>!state.busy&&localDrafts.ready&&state.databases.length);
  await page.addInitScript(()=>{window.uploadReadSizes=[];const original=Blob.prototype.arrayBuffer;Blob.prototype.arrayBuffer=function(){window.uploadReadSizes.push(this.size);return original.call(this);};});
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(origin);await idle();
  check(await page.evaluate(()=>state.dataDir.includes('workflow-lab')),'isolated fixture');
  await page.locator('#database-select').selectOption('plant_a.db');await idle();
  const original=await page.evaluate(()=>state.records[0].row.value);
  const cell=page.locator('#data-grid td[data-cell-row="0"][data-cell-field="value"]');
  await cell.dblclick();await cell.locator('input').fill(String(original+1));await cell.locator('input').press('Enter');
  const open=async()=>{
    await page.locator('#table-list').click({button:'right'});await page.getByRole('menuitem',{name:'整库快照 / 恢复',exact:true}).click();await idle();
    await page.locator('#snapshot-upload').click();await idle();
  };
  await open();check(await page.locator('#chunk-upload-dialog').isVisible(),'upload entry opens');
  check(await page.evaluate(()=>state.pendingEdits.size===1),'opening uploader preserves pending cell edit');
  const file='D:/codex/rtdb/output/playwright/upload-large.db';await page.locator('#chunk-file').setInputFiles(file);
  let release,entered,hold=true,lost=false;const gate=new Promise(resolve=>release=resolve),arrived=new Promise(resolve=>entered=resolve);
  await page.route('**/database-uploads/*/chunk?*',async route=>{
    const offset=Number(new URL(route.request().url()).searchParams.get('offset'));offsets.push(offset);
    if(offset===1048576&&hold){entered();await gate;try{await route.abort('aborted');}catch{};return;}
    if(offset===2097152&&!lost){lost=true;await route.fetch();await route.abort('failed');return;}
    await route.continue();
  });
  try{
    await page.locator('#chunk-start').click();await arrived;
    await page.locator('#chunk-pause').click();release();
    await page.waitForFunction(()=>!databaseUploads.active);
    const ident=await page.evaluate(()=>databaseUploads.session.id);
    let current=await(await page.request.get(origin+'/api/manage/database-uploads/'+ident)).json();
    check(current.received===1048576,'pause retains one acknowledged chunk');
    check(current.size>16*1024**2,'file exceeds old 16 MiB limit');
    check((await page.locator('#chunk-status').innerText()).includes('已暂停'),'pause status visible');
    check(await page.evaluate(()=>state.pendingEdits.size===1),'uploading preserves pending edit');
    await page.evaluate(async()=>{localDrafts.suspended=true;clearTimeout(localDrafts.timer);await draftStore.queue;state.pendingEdits.clear();await localDrafts.dropKey(localDrafts.key());});
    hold=false;offsets.length=0;
    await page.reload();await idle();await open();
    check(await page.evaluate(()=>databaseUploads.session.id)===ident,'reload recovers upload identity');
    check(await page.locator('#chunk-start').isDisabled(),'reload requires reselecting local file');
    await page.locator('#chunk-file').setInputFiles(file);await page.locator('#chunk-start').click();
    await page.waitForFunction(()=>!databaseUploads.active&&!state.busy,{},{timeout:120000});
    check((await page.locator('#chunk-status').innerText()).includes('校验通过'),'large upload validates successfully');
    check(!offsets.includes(0),'confirmed chunk is not retransmitted');
    check(offsets.filter(value=>value===2097152).length===2,'lost acknowledgement retries same chunk');
    current=await(await page.request.get(origin+'/api/manage/database-uploads/'+ident)).json();
    check(current.state==='ready'&&current.received===current.size,'final size has no duplicated bytes');
    check(current.result.table_count===2,'uploaded snapshot includes all tables');
    check(await page.evaluate(()=>Math.max(...window.uploadReadSizes)<=1048576),'browser reads at most 1 MiB per slice');
    await page.locator('#chunk-target').fill('plant_a');await page.locator('#chunk-restore').click();await idle();
    check((await page.locator('#chunk-status').innerText()).includes('已存在'),'same-name restore rejected');
    const target='upload_ui_'+Date.now();await page.locator('#chunk-target').fill(target);await page.locator('#chunk-label').fill('大文件上传验证');
    await page.locator('#chunk-restore').click();await idle();
    check((await page.locator('#chunk-status').innerText()).includes('已恢复为 '+target),'restore completion visible');
    check(await page.locator(`#database-select option[value="${target}.db"]`).count()===1,'restored database appears in selector');
    const rows=await(await page.request.get(origin+`/api/manage/tables/nodes/rows?database=${target}.db&limit=1`)).json();
    check(rows.total===250&&rows.records[0].row.name==='上传节点0','restored rows verified');
    const schema=await(await page.request.get(origin+`/api/manage/tables/nodes/schema?database=${target}.db`)).json();
    check(schema.definition.columns[1].default_value==='默认名称','column defaults preserved');
    const untouched=await(await page.request.get(origin+'/api/manage/tables/devices/rows?database=plant_a.db&limit=1')).json();
    check(untouched.records[0].row.value===original,'existing business fixture untouched');
    await page.screenshot({path:'output/playwright/database-upload-complete.png'});
    await page.setViewportSize({width:430,height:900});
    check(await page.locator('#chunk-upload-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth+1),'upload dialog fits narrow viewport');
    await page.screenshot({path:'output/playwright/database-upload-mobile.png'});
    await page.setViewportSize({width:1280,height:720});
    check(errors.length===0,'no browser exceptions');
    return {passed:checks.length,checks,upload:ident,bytes:current.size,restored:target+'.db'};
  }finally{release();await page.unrouteAll({behavior:'wait'});}
}
