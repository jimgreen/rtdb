// Only the isolated workflow-lab fixture; restore every changed value/configuration.
async(page)=>{
  const origin=new URL(page.url()).origin,checks=[],errors=[],requests=[];
  const check=(value,name)=>{if(!value)throw Error(name);checks.push(name);};
  const idle=()=>page.waitForFunction(()=>!state.busy&&localDrafts.ready&&state.databases.length);
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>requests.push({method:request.method(),url:request.url()}));
  await page.goto(origin);await idle();
  check(await page.evaluate(()=>state.dataDir.includes('workflow-lab')),'isolated fixture');
  await page.locator('#database-select').selectOption('plant_a.db');await idle();
  const original=await page.evaluate(()=>structuredClone(state.records[0].row));
  const suffix='?database=plant_a.db',policy=await(await page.request.get(origin+'/api/manage/receipt-maintenance'+suffix)).json();
  let release,popup;const gate=new Promise(resolve=>release=resolve);
  try{
    await page.locator('#table-list').click({button:'right'});
    await page.getByRole('menuitem',{name:'日志与回执维护',exact:true}).click();await idle();
    check(await page.locator('#receipt-maintenance-dialog').isVisible(),'maintenance entry opens');
    check((await page.locator('#receipt-database').innerText())==='plant_a.db','maintenance bound to chosen database');
    await page.locator('#receipt-days').fill('14');await page.locator('#receipt-config').click();await idle();
    check((await(await page.request.get(origin+'/api/manage/receipt-maintenance'+suffix)).json()).days===14,'compression policy persisted');
    check((await(await page.request.get(origin+'/api/manage/receipt-maintenance?database=plant_b.db')).json()).days===7,'other database policy unchanged');
    await page.locator('#receipt-archive').click();await idle();
    check((await page.locator('#receipt-statistics').innerText()).includes('已压缩回执'),'receipt statistics displayed');
    await page.screenshot({path:'output/playwright/receipt-maintenance.png'});
    await page.locator('#receipt-close').click();
    await page.route('**/api/manage/tables/devices/cell-edits?**',async route=>{
      const response=await route.fetch();let body=await response.json();
      if(response.status()===200){
        const url=new URL(route.request().url());
        const actual=await(await page.request.get(origin+'/api/manage/tasks/'+url.searchParams.get('request_id')+suffix)).json();
        body={task:{...actual,status:'running',result:undefined,revision:0}};
      }
      await route.fulfill({status:202,contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.route('**/api/manage/tasks/*',async route=>{
      if(new URL(route.request().url()).searchParams.has('wait'))await gate;
      await route.continue();
    });
    const cell=page.locator('#data-grid td[data-cell-row="0"][data-cell-field="value"]');
    await cell.dblclick();await cell.locator('input').fill(String(original.value+.125));await cell.locator('input').press('Enter');
    requests.length=0;await cell.click();await page.keyboard.press('Control+s');
    await page.locator('#task-dialog').waitFor({state:'visible'});
    check(requests.filter(r=>r.url.includes('/cell-edits')&&r.method==='PUT').length===1,'save uses one PUT');
    check(!requests.some(r=>r.url.includes('/validate-edits')),'save does not repeat validation HTTP call');
    check(await page.locator('#task-browse').getAttribute('rel')==='noopener noreferrer','separate browsing isolates task session');
    const opened=page.context().waitForEvent('page');await page.locator('#task-browse').click();popup=await opened;
    await popup.waitForFunction(()=>!state.busy&&state.databases.length);
    await popup.locator('#database-select').selectOption('plant_b.db');
    await popup.waitForFunction(()=>!state.busy&&state.records.length);
    check((await popup.locator('#current-database-file').innerText())==='plant_b.db','another database can be browsed while original task is tracked');
    check(await popup.evaluate(()=>!backgroundTasks.pending()),'new page does not inherit original task identity');
    await popup.close();popup=null;release();await idle();
    check(requests.some(r=>r.url.includes('/tasks/')&&r.url.includes('wait=2')),'client uses revision long polling');
    check(await page.evaluate(()=>state.records[0].row.value)===original.value+.125,'saved value displayed');
    check(await page.evaluate(()=>state.pendingEdits.size===0&&!backgroundTasks.pending()),'successful save clears matching draft');
    check(await page.evaluate(()=>uiPerformance.samples.some(s=>s.label==='保存并刷新'&&s.validate_ms>=0&&s.transaction_ms>=0&&s.queue_ms>=0)),'save phases and full refresh timing recorded');
    check(errors.length===0,'no page errors');
    return {passed:checks.length,checks};
  }finally{
    release();if(popup)await popup.close();await page.unrouteAll({behavior:'wait'});
    await page.request.put(origin+'/api/manage/receipt-maintenance'+suffix,{data:{days:policy.days}});
    const latest=await(await page.request.get(origin+'/api/manage/tables/devices/rows'+suffix+'&limit=1')).json();
    const response=await page.request.put(origin+'/api/manage/tables/devices/cell-edits'+suffix,{data:{changes:[{ref:latest.records[0].ref,values:{value:original.value}}]}});
    if(!response.ok())throw Error('Fixture restoration failed: '+await response.text());
  }
}
