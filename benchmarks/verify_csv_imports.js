async(page)=>{
  const checks=[],errors=[],offsets=[];let release;
  const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
  page.on('pageerror',error=>errors.push(error.message));
  const origin=new URL(page.url()).origin;
  await page.addInitScript(()=>{window.csvReadSizes=[];const original=Blob.prototype.arrayBuffer;Blob.prototype.arrayBuffer=function(){window.csvReadSizes.push(this.size);return original.call(this);};});
  await page.reload();const idle=()=>page.waitForFunction(()=>typeof state!=='undefined'&&!state.busy&&localDrafts.ready&&state.databases.length);await idle();
  check(await page.evaluate(()=>state.dataDir.includes('workflow-lab')),'isolated lab');
  const database=await page.locator('#database-select option').evaluateAll(options=>options.map(o=>o.value).filter(v=>v.startsWith('csv_ui_')).sort().at(-1));
  await page.locator('#database-select').selectOption(database);await idle();
  const open=async()=>{await page.locator('#table-list').click({button:'right'});await page.getByRole('menuitem',{name:'CSV 流式导入',exact:true}).click();await idle();};
  await open();check(await page.locator('#csv-dialog').isVisible(),'context menu entry');
  if(await page.locator('#csv-discard').isEnabled()){await page.locator('#csv-discard').click();await idle();}
  await page.locator('#csv-file').setInputFiles('D:/codex/rtdb/output/playwright/csv-large.csv');
  let arrived,hold=true,lost=false;const gate=new Promise(r=>release=r),reached=new Promise(r=>arrived=r);
  await page.route('**/database-uploads/*/chunk?*',async route=>{
    const offset=Number(new URL(route.request().url()).searchParams.get('offset'));offsets.push(offset);
    if(offset===1048576&&hold){arrived();await gate;try{await route.abort();}catch{}return;}
    if(offset===2097152&&!lost){lost=true;await route.fetch();await route.abort('failed');return;}
    await route.continue();
  });
  try{
    await page.locator('#csv-start').click();await reached;await page.locator('#csv-pause').click();release();await page.waitForFunction(()=>!csvImports.active);
    const ident=await page.evaluate(()=>csvImports.session.id);
    let value=await(await page.request.get(origin+'/api/manage/database-uploads/'+ident)).json();
    check(value.received===1048576&&value.size>16*1024**2,'large file pause retains acknowledged data');
    hold=false;offsets.length=0;
    await page.reload();await idle();await page.locator('#database-select').selectOption(database);await idle();await open();
    check(await page.evaluate(()=>csvImports.session.id)===ident,'reload recovers session');
    check(await page.locator('#csv-start').isDisabled(),'reselect file required');
    await page.locator('#csv-file').setInputFiles('D:/codex/rtdb/output/playwright/csv-large.csv');await page.locator('#csv-start').click();
    await page.waitForFunction(()=>!csvImports.active&&!state.busy,{}, {timeout:120000});
    check((await page.locator('#csv-status').innerText()).includes('校验通过'),'preview succeeds');
    check(!offsets.includes(0),'confirmed bytes not resent');check(offsets.filter(n=>n===2097152).length===2,'lost acknowledgement retried once');
    check(await page.locator('#csv-preview tbody tr').count()===20,'preview limited to 20 samples');
    check((await page.locator('#csv-preview thead').innerText()).includes('FLOAT'),'preview shows field types');
    check(await page.evaluate(()=>Math.max(...window.csvReadSizes)<=1048576),'bounded browser reads');
    const rows=async(table='devices')=>(await(await page.request.get(origin+`/api/manage/tables/${table}/rows?database=${database}&limit=2`)).json());
    check((await rows()).total===1,'preview leaves target unchanged');
    await page.screenshot({path:'output/playwright/csv-preview.png'});
    await page.setViewportSize({width:430,height:900});
    check(await page.locator('#csv-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth+1),'narrow dialog fits');
    await page.screenshot({path:'output/playwright/csv-mobile.png'});await page.setViewportSize({width:1280,height:720});
    await page.locator('#csv-apply').click();await page.waitForFunction(()=>!csvImports.active&&!state.busy,{}, {timeout:120000});
    check((await page.locator('#csv-status').innerText()).includes('导入成功：18000'),'import completion visible');
    value=await rows();check(value.total===18001,'all imported records counted');
    check(value.records[0].row.name==='原始记录'&&value.records[1].row.name.startsWith('带逗号,换行\n名称'),'old and quoted data preserved');
    check((await rows('samples')).total===1,'sibling table unaffected');
    await page.locator('#csv-apply').click();await page.waitForFunction(()=>!csvImports.active&&!state.busy);
    check((await page.locator('#csv-status').innerText()).includes('未重复写入'),'duplicate confirm is idempotent');
    check((await rows()).total===18001,'no duplicate data after replay');
    await page.screenshot({path:'output/playwright/csv-import-complete.png'});
    check(errors.length===0,'no JavaScript errors');
    return {passed:checks.length,checks,database,upload:ident,rows:18000};
  }finally{release();await page.unrouteAll({behavior:'wait'});}
}
