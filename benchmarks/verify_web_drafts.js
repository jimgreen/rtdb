// Only against benchmarks/serve_workflow_lab.py.
async(page)=>{
  const checks=[],errors=[],origin=new URL(page.url()).origin;
  const check=(condition,message)=>{if(!condition)throw Error(message);checks.push(message);};
  const idle=p=>p.waitForFunction(()=>!state.busy&&localDrafts.ready&&state.databases.length);
  const edit=async(p,value)=>{const td=p.locator('#data-grid td[data-cell-row="0"][data-cell-field="value"]');await td.dblclick();await td.locator('input').fill(value);await td.locator('input').press('Enter');await p.evaluate(()=>localDrafts.flush());};
  const reload=async p=>{await p.evaluate(async()=>{await localDrafts.flush();localDrafts.suspended=true;state.pendingEdits.clear();state.records=state.records.filter(r=>!r.draftId);state.schemaDirty=false;});await p.reload();await idle(p);await p.locator('#database-select').selectOption('plant_a.db');await idle(p);};
  page.on('pageerror',e=>errors.push(e.message));await page.evaluate(()=>{if(!state.dataDir.includes('workflow-lab'))throw Error('fixture only');localDrafts.suspended=true;state.pendingEdits.clear();state.schemaDirty=false;});await page.goto(origin);await idle(page);
  await page.evaluate(async()=>{if(!state.dataDir.includes('workflow-lab'))throw Error('fixture only');for(const key of draftStore.cache.keys())await localDrafts.dropKey(key);});
  await page.locator('#database-select').selectOption('plant_a.db');await idle(page);
  await page.request.post(origin+'/api/manage/tables/devices/sql?database=plant_a.db',{data:{sql:'UPDATE devices SET value=10.1 WHERE id=1',confirm:'devices'}});await page.evaluate(()=>queryRows());
  const original=await page.evaluate(()=>structuredClone(state.records[0].row));
  const second=await page.context().newPage();second.on('pageerror',e=>errors.push(e.message));
  try{
    await second.goto(origin);await idle(second);await second.locator('#database-select').selectOption('plant_a.db');await idle(second);
    await edit(page,'21.25');await edit(second,'31.25');
    check(await page.evaluate(()=>localDrafts.owner)!==await second.evaluate(()=>localDrafts.owner),'pages have distinct draft identities');
    await page.evaluate(()=>draftStore.refresh());
    check(await page.evaluate(()=>localDrafts.candidates('data').length===2),'two drafts coexist in IndexedDB');
    await reload(page);
    check(await page.locator('#draft-recovery-dialog').isVisible(),'reload offers inactive draft');
    check(await page.locator('#draft-choice option').count()===1,'other active page excluded from automatic recovery');
    await page.locator('#restore-local-draft').click();await idle(page);
    check(await page.evaluate(()=>state.records[0].row.value===21.25),'restored own value rather than other page value');
    await page.locator('#data-grid td[data-cell-row="0"][data-cell-field="value"]').click();await page.keyboard.press('Control+s');await idle(page);
    check(await second.evaluate(()=>localDrafts.read(localDrafts.key()).layout[0].edits.some(e=>e.raw==='31.25')),'saving one page preserves other draft');
    await second.locator('#data-grid td[data-cell-row="0"][data-cell-field="value"]').click();await second.keyboard.press('Control+s');await idle(second);
    check(await second.locator('#save-warning-dialog').isVisible(),'other page save checks current server version');
    await second.locator('#save-conflict-grid select').selectOption('server');await second.locator('#apply-conflict-choices').click();
    await second.evaluate(()=>draftStore.queue);
    // Keep invalid text too, so recovery does not silently normalize unfinished edits.
    await page.locator('#nav-schema').click();await page.locator('#schema-label').fill('结构草稿检查');
    const defaultValue=page.locator('#schema-fields [aria-label="value default_value"]');await defaultValue.locator('..').dblclick();await defaultValue.fill('not-a-number');await defaultValue.press('Tab');
    await page.evaluate(()=>localDrafts.flush());await reload(page);
    const sourceKey=await page.evaluate(()=>localDrafts.candidates('schema')[0].key);
    await page.evaluate(()=>{window.originalDraftPut=draftStore.put;draftStore.put=async()=>{throw new DOMException('test quota exhausted','QuotaExceededError');};});
    await page.locator('#restore-local-draft').click();await idle(page);
    check(await page.locator('#schema-label').inputValue()==='结构草稿检查','schema label restored');
    check(await defaultValue.inputValue()==='not-a-number','unfinished invalid default text preserved');
    check(await page.evaluate(()=>state.schemaDirty),'restored structure remains unsaved');
    await page.evaluate(()=>draftStore.refresh());
    check(await page.evaluate(key=>draftStore.cache.has(key),sourceKey),'failed recovery persistence preserves original IndexedDB draft');
    check((await page.locator('#operation-message').textContent()).includes('本地草稿未能保存'),'quota failure shown to user');
    await page.evaluate(async key=>{draftStore.put=window.originalDraftPut;await localDrafts.flush();await localDrafts.dropKey(key);},sourceKey);
    await page.locator('#refresh').click();await page.locator('#confirm-action').click();await idle(page);
    check(await page.evaluate(()=>!localDrafts.candidates('schema').length),'explicit discard clears own schema draft');
    const result=await page.request.post(origin+'/api/manage/tables/devices/sql?database=plant_a.db',{data:{sql:`UPDATE devices SET value=${original.value} WHERE id=${original.id}`,confirm:'devices'}});
    check(result.ok(),'fixture restored');check(errors.length===0,'no browser exceptions');
    return {passed:checks.length,checks,errors};
  }finally{await second.evaluate(()=>{localDrafts.suspended=true;state.pendingEdits.clear();state.schemaDirty=false;});await second.close();}
}
