// Run only against serve_workflow_lab.py fixtures, never business databases.
async (page) => {
  const origin=new URL(page.url()).origin,checks=[],errors=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
  const idle=()=>page.waitForFunction(()=>!state.busy&&state.databases.length);
  // Flush the real draft, then bypass only the leave-page warning in this fixture.
  const reload=async()=>{await page.evaluate(async()=>{await localDrafts.flush();localDrafts.suspended=true;state.pendingEdits.clear();state.records=state.records.filter(r=>!r.draftId);state.schemaDirty=false;});await page.reload();await idle();};
  const cell=(row,field)=>page.locator(`#data-grid td[data-cell-row="${row}"][data-cell-field="${field}"]`);
  const edit=async(row,field,value)=>{await cell(row,field).dblclick();const input=cell(row,field).locator('input');await input.fill(value);await input.press('Enter');};
  const remote=async sql=>{const r=await page.request.post(origin+'/api/manage/tables/devices/sql?database=plant_a.sqlite3',{data:{sql,confirm:'devices'}});if(!r.ok())throw Error(await r.text());return r.json();};
  const choose=async()=>{await page.locator('#database-select').selectOption('plant_a.sqlite3');await idle();};
  const add=async id=>{
    await cell(4,'value').click({button:'right'});await page.locator('[data-action="add-row"]').click();
    await page.locator('#add-rows-form button[type="submit"]').click();await idle();
    await edit(5,'id',String(id));await edit(5,'name','新行草稿');await edit(5,'value','1.5');await edit(5,'other','2.5');
  };
  await page.evaluate(async()=>{if(!state.dataDir.includes('workflow-lab'))throw Error('fixture required');localDrafts.suspended=true;state.pendingEdits.clear();state.records=state.records.filter(r=>!r.draftId);await draftStore.queue;for(const key of draftStore.cache.keys())await localDrafts.dropKey(key);backgroundTasks.clear();});
  await page.goto(origin);await idle();
  await remote('DELETE FROM devices WHERE id IN (9001,9002)');
  await remote("UPDATE devices SET value=10.5,other=11.0,name='baseline' WHERE id=5");
  await choose();await edit(4,'value','25.5');await edit(4,'name','恢复草稿');await add(9001);
  await page.waitForFunction(()=>localDrafts.read()?.layout.some(r=>r.draftId));
  check(await page.evaluate(()=>localDrafts.read().layout.filter(r=>r.edits?.length||r.draftId).length===2),'existing edits and inserted row persisted');
  await reload();await choose();
  check(await page.locator('#draft-recovery-dialog').isVisible(),'reload offers local draft');
  await remote('UPDATE devices SET value=30.5,other=40.5 WHERE id=5');
  await page.locator('#restore-local-draft').click();await idle();
  check(await page.locator('#save-warning-dialog').isVisible(),'restore detects remote version conflict');
  const conflicts=await page.locator('#save-conflict-grid').innerText();
  check(conflicts.includes('10.5')&&conflicts.includes('30.5')&&conflicts.includes('25.5'),'restored conflict shows baseline server and local values');
  check((await remote('SELECT * FROM devices WHERE id=9001')).rows.length===0,'restoring draft never inserts data');
  for(const select of await page.locator('#save-conflict-grid select').all())await select.selectOption('local');
  await page.locator('#apply-conflict-choices').click();
  check(await page.evaluate(()=>state.records.find(r=>r.row.id===5).row.other===40.5),'conflict choice preserves unrelated remote fields');
  check(await cell(4,'value').evaluate(e=>e.classList.contains('cell-modified')),'restored changes highlighted');
  await cell(4,'value').click();await page.keyboard.press('Control+s');await idle();
  check(await page.evaluate(()=>!hasPendingData()&&!localDrafts.read()),'confirmed save clears matching draft');
  check((await remote('SELECT * FROM devices WHERE id=9001')).rows.length===1,'restored inserted record saved once');
  // A faster save may return before the durable audit marker changes data_version.
  // Verify cursor use on a stable snapshot; concurrent-change fallback has backend tests.
  await page.waitForFunction(async()=>!(await(await fetch('/api/manage/audit-delivery')).json()).pending_databases.length);
  await page.evaluate(()=>queryRows());await idle();
  await page.locator('#next').click();await idle();
  check(await page.evaluate(()=>uiPerformance.latest.pagination==='cursor'&&state.offset===100),'next page uses cursor');
  await page.locator('#prev').click();await idle();
  check(await page.evaluate(()=>uiPerformance.latest.pagination==='cursor'&&state.offset===0),'previous page uses cursor');
  check(await page.evaluate(()=>uiPerformance.latest.query_ms>=0&&uiPerformance.latest.decode_ms>=0&&uiPerformance.latest.encode_ms>=0),'performance includes backend phases and encoding');
  await page.locator('#last-refresh').dblclick();
  check((await page.locator('#performance-results').innerText()).includes('SQL/取数'),'performance phases visible');
  await page.screenshot({path:'output/playwright/query-phases.png'});
  await page.locator('#performance-dialog button[value="close"]').first().click();
  // Commit succeeds but its HTTP response is lost. Reload must find the same receipt.
  await add(9002);await page.route('**/api/manage/tables/devices/cell-edits?*',async route=>{await route.fetch();await route.abort('failed');});
  await cell(5,'value').click();await page.keyboard.press('Control+s');await idle();
  check(await page.evaluate(()=>backgroundTasks.pending()&&!!localDrafts.read()),'lost response retains request identity and draft');
  await page.unroute('**/api/manage/tables/devices/cell-edits?*');await reload();
  check(await page.evaluate(()=>!backgroundTasks.pending()),'reload resolves existing task receipt');
  await choose();
  check(!await page.locator('#draft-recovery-dialog').isVisible(),'confirmed recovered save clears unchanged draft');
  check((await remote('SELECT * FROM devices WHERE id=9002')).rows.length===1,'lost-response recovery does not duplicate insert');
  await page.locator('#nav-logs').click();await idle();await page.locator('#task-history').click();await idle();
  check((await page.locator('#task-history-grid').innerText()).includes('cell-edits'),'durable task history visible');
  await page.screenshot({path:'output/playwright/task-history.png'});
  await page.getByRole('button',{name:'关闭任务列表',exact:true}).click();
  // Structure mismatch blocks recovery while allowing draft export.
  await page.locator('#nav-chart').click();await edit(4,'value','45.5');
  await page.waitForFunction(()=>!!localDrafts.read());
  const schema=await (await page.request.get(origin+'/api/manage/tables/devices/schema?database=plant_a.sqlite3')).json();
  const definition=structuredClone(schema.definition);definition.head.chn_name+='恢复检查';
  const changed=await page.request.put(origin+'/api/manage/tables/devices/schema?database=plant_a.sqlite3',{data:{definition,version:schema.version}});check(changed.ok(),'fixture schema changed');
  await reload();await choose();
  check(await page.locator('#restore-local-draft').isDisabled(),'schema mismatch blocks unsafe draft restore');
  check(await page.locator('#export-local-draft').isEnabled(),'mismatched draft remains exportable');
  await page.locator('#delete-local-draft').click();
  const latest=await (await page.request.get(origin+'/api/manage/tables/devices/schema?database=plant_a.sqlite3')).json();
  await page.request.put(origin+'/api/manage/tables/devices/schema?database=plant_a.sqlite3',{data:{definition:schema.definition,version:latest.version}});
  await remote('DELETE FROM devices WHERE id IN (9001,9002)');await remote("UPDATE devices SET value=10.5,other=11,name='plant_a' WHERE id=5");
  check(errors.length===0,'no browser exceptions');return {passed:checks.length,checks,errors};
}
