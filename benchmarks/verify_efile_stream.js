async(page)=>{
  const checks=[],errors=[],offsets=[];const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{window.eReadSizes=[];const original=Blob.prototype.arrayBuffer;Blob.prototype.arrayBuffer=function(){window.eReadSizes.push(this.size);return original.call(this);};});
  await page.reload();const idle=()=>page.waitForFunction(()=>typeof state!=='undefined'&&!state.busy&&localDrafts.ready&&state.databases.length);await idle();
  check(await page.evaluate(()=>state.dataDir.includes('workflow-lab')),'isolated database only');
  const database=await page.locator('#database-select option').evaluateAll(items=>items.map(i=>i.value).filter(v=>v.startsWith('e_ui_')).sort().at(-1));
  await page.locator('#database-select').selectOption(database);await idle();
  const open=async()=>{await page.locator('#table-list').click({button:'right'});await page.getByRole('menuitem',{name:'加载 dbdef / E',exact:true}).click();await idle();};
  await open();if(await page.locator('#efile-discard').isEnabled()){await page.locator('#efile-discard').click();await idle();}
  await page.locator('#definition-file').setInputFiles('D:/codex/rtdb/output/playwright/multi-large.e');
  let release,arrived,hold=true,lost=false;const gate=new Promise(r=>release=r),reached=new Promise(r=>arrived=r);
  await page.route('**/database-uploads/*/chunk?*',async route=>{
    const offset=Number(new URL(route.request().url()).searchParams.get('offset'));offsets.push(offset);
    if(offset===1048576&&hold){arrived();await gate;try{await route.abort();}catch{}return;}
    if(offset===2097152&&!lost){lost=true;await route.fetch();await route.abort('failed');return;}
    await route.continue();
  });
  try{
    await page.locator('#preview-definition').click();await reached;await page.locator('#efile-pause').click();release();await idle();
    check(await page.evaluate(()=>efileImports.session.received)===1048576,'pause retains acknowledged block');
    const ident=await page.evaluate(()=>efileImports.session.id);hold=false;offsets.length=0;
    await page.reload();await idle();await page.locator('#database-select').selectOption(database);await idle();await open();
    check(await page.evaluate(()=>efileImports.session.id)===ident,'reload restores upload ID');
    await page.locator('#definition-file').setInputFiles('D:/codex/rtdb/output/playwright/multi-large.e');await page.locator('#preview-definition').click();await idle();
    check(await page.locator('.definition-table-tab').count()===2,'one tab per table');
    check(!offsets.includes(0)&&offsets.filter(o=>o===2097152).length===2,'resume and lost acknowledgement retry');
    check(await page.evaluate(()=>Math.max(...eReadSizes))<=1048576,'browser reads at most one MiB');
    check(await page.locator('#apply-definition').isDisabled(),'cannot apply before complete validation');
    check(await page.locator('#definition-table-panel-0 [data-definition-field="code"] input').isChecked(),'first field is key rather than later id');
    check(await page.locator('#definition-table-panel-0 [data-definition-field="value"] select').inputValue()==='float','last row participates in inferred type');
    check(await page.evaluate(()=>state.tables.length)===0,'parse leaves target database unchanged');
    await page.locator('#definition-table-panel-0 [data-definition-field="value"] select').selectOption('str');
    await page.locator('#preview-definition').click();await idle();
    check(await page.locator('#apply-definition').isEnabled(),'edited type passes complete validation');
    await page.locator('#definition-mode').selectOption('append');check(await page.locator('#apply-definition').isDisabled(),'mode change invalidates confirmation');
    await page.locator('#preview-definition').click();await idle();
    await page.screenshot({path:'output/playwright/efile-preview.png'});
    await page.setViewportSize({width:430,height:900});
    const fits=await page.locator('#definition-dialog').evaluate(e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;});
    check(fits,'dialog fits narrow viewport');await page.screenshot({path:'output/playwright/efile-mobile.png'});await page.setViewportSize({width:1280,height:720});
    await page.locator('#apply-definition').click();
    await page.locator('#confirm-input').fill(database);await page.locator('#confirm-action').click();await idle();
    check((await page.locator('#efile-status').innerText()).includes('成功 6000 条'),'multi-table import succeeds');
    const response=await page.request.get(new URL(page.url()).origin+'/api/manage/tables?database='+database),tables=(await response.json()).tables;
    check(tables.length===2&&tables.every(t=>t.row_count===6000),'both tables have exact row counts');
    check(tables.find(t=>t.name==='meters').definition.columns.find(c=>c.eng_name==='value').data_type==='str','manual type persisted');
    await page.locator('#apply-definition').click();await page.locator('#confirm-input').fill(database);await page.locator('#confirm-action').click();await idle();
    check((await page.locator('#efile-status').innerText()).split('跳过重复执行').length===3,'duplicate confirmation skips both committed tables');
    await page.screenshot({path:'output/playwright/efile-complete.png'});
    check(errors.length===0,'no JavaScript exceptions');return {checks,errors,database};
  }finally{release();await page.unrouteAll();}
}
