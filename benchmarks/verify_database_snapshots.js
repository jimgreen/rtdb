// Isolated workflow-lab only. Creates a uniquely named copy, preserving source data.
async(page)=>{
  const origin=new URL(page.url()).origin,checks=[],errors=[];
  const check=(value,name)=>{if(!value)throw Error(name);checks.push(name);};
  const idle=()=>page.waitForFunction(()=>!state.busy&&localDrafts.ready&&state.databases.length);
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(origin);await idle();
  check(await page.evaluate(()=>state.dataDir.includes('workflow-lab')),'isolated fixture');
  await page.locator('#table-list').click({button:'right'});
  await page.getByRole('menuitem',{name:'整库快照 / 恢复',exact:true}).click();await idle();
  check(await page.locator('#database-snapshots-dialog').isVisible(),'backup list accessible before database selection');
  check(await page.locator('#snapshot-create').isDisabled(),'creation needs selected database');
  await page.locator('#snapshot-close').click();
  await page.locator('#database-select').selectOption('plant_a.db');await idle();
  const original=await page.evaluate(()=>state.records[0].row.value);
  const cell=page.locator('#data-grid td[data-cell-row="0"][data-cell-field="value"]');
  await cell.dblclick();await cell.locator('input').fill(String(original+1));await cell.locator('input').press('Enter');
  try{
    await page.locator('#table-list').click({button:'right'});
    await page.getByRole('menuitem',{name:'整库快照 / 恢复',exact:true}).click();await idle();
    await page.locator('#snapshot-create').click();await idle();
    check((await page.locator('#snapshot-status').innerText()).includes('快照已生成'),'snapshot completion visible');
    check(await page.evaluate(()=>state.pendingEdits.size===1),'snapshot preserves unsaved draft');
    const result=await(await page.request.get(origin+'/api/manage/database-snapshots?database=plant_a.db')).json(),item=result.snapshots[0];
    check(item.table_count===2&&item.tables.includes('devices')&&item.tables.includes('samples'),'whole database includes both tables');
    const row=page.locator('#snapshot-list tr').filter({has:page.locator(`a[href*="${item.id}"]`)});
    const downloadEvent=page.waitForEvent('download');await row.getByRole('link',{name:'下载',exact:true}).click();
    const downloaded=await downloadEvent;await downloaded.saveAs('output/playwright/snapshot-ui-download.db');
    check((await downloaded.failure())===null,'browser downloads SQLite file');
    const ranged=await page.request.get(origin+`/api/manage/database-snapshots/${item.id}/download?database=plant_a.db`,{headers:{Range:'bytes=0-15'}});
    check(ranged.status()===206&&(await ranged.body()).toString()==='SQLite format 3\0','range download has SQLite header');
    await row.getByRole('button',{name:'选为恢复源',exact:true}).click();
    await page.locator('#snapshot-target-name').fill('plant_b');await page.locator('#snapshot-restore').click();await idle();
    check((await page.locator('#snapshot-status').innerText()).includes('已存在'),'existing database conflict is shown');
    const target='snapshot_ui_'+Date.now();await page.locator('#snapshot-target-name').fill(target);
    await page.locator('#snapshot-target-label').fill('快照恢复验证');await page.locator('#snapshot-restore').click();await idle();
    check((await page.locator('#snapshot-status').innerText()).includes('已恢复为 '+target+'.db'),'restore success visible');
    check(await page.locator(`#database-select option[value="${target}.db"]`).count()===1,'new database appears in selector');
    check(await page.evaluate(()=>state.pendingEdits.size===1&&state.database==='plant_a.db'),'restore preserves original page and draft');
    const copy=await(await page.request.get(origin+`/api/manage/tables/devices/rows?database=${target}.db&limit=1`)).json();
    check(copy.records[0].row.value===original,'snapshot contains committed value only');
    const source=await(await page.request.get(origin+'/api/manage/tables/devices/rows?database=plant_a.db&limit=1')).json();
    check(source.records[0].row.value===original,'source database untouched');
    await page.screenshot({path:'output/playwright/database-snapshots.png'});
    await page.setViewportSize({width:430,height:900});
    check(await page.locator('#database-snapshots-dialog').evaluate(dialog=>dialog.getBoundingClientRect().width<=innerWidth),'dialog fits narrow viewport');
    await page.screenshot({path:'output/playwright/database-snapshots-mobile.png'});
    await page.setViewportSize({width:1280,height:720});
    check(errors.length===0,'no browser exceptions');
    return {passed:checks.length,checks,snapshot:item.id,restored:target+'.db'};
  }finally{
    await page.evaluate(async()=>{localDrafts.suspended=true;clearTimeout(localDrafts.timer);await draftStore.queue;state.pendingEdits.clear();await localDrafts.dropKey(localDrafts.key('data'));});
  }
}
