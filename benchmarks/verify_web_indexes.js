// Run only on the disposable workflow-lab service.
async(page)=>{
  const origin=new URL(page.url()).origin,checks=[],errors=[];
  const check=(ok,label)=>{if(!ok)throw Error(label);checks.push(label);};
  const idle=()=>page.waitForFunction(()=>!state.busy&&localDrafts.ready&&state.databases.length);
  page.on('pageerror',error=>errors.push(error.message));await page.goto(origin);await idle();
  check(await page.evaluate(()=>state.dataDir.includes('workflow-lab')),'isolated fixture verified');
  const name='ui_indexes_'+Date.now(),suffix='?database=plant_a.sqlite3';
  const original=await (await page.request.get(origin+'/api/manage/tables/devices/schema'+suffix)).json();
  const definition=structuredClone(original.definition);definition.head={eng_name:name,chn_name:'索引功能验证'};
  const created=await page.request.put(origin+'/api/manage/tables/'+name+'/schema'+suffix,{data:{creating:true,definition}});
  check(created.ok(),'scoped table created with receipt');
  try{
    await page.locator('#database-select').selectOption('plant_a.sqlite3');await idle();
    await page.getByRole('button',{name:new RegExp(name)}).click();await idle();await page.locator('#nav-schema').click();
    const menu=async action=>{await page.locator('#schema-fields tbody tr').first().click({button:'right'});await page.locator('[data-schema-action="'+action+'"]').click();};
    await menu('indexes');check(await page.locator('#schema-index-dialog').isVisible(),'index editor opens from context menu');
    await page.locator('#index-mode').selectOption('custom');await page.locator('#add-index').click();
    const fields=page.locator('#index-rows select');await fields.selectOption('name');await fields.selectOption('value');
    check((await page.locator('#index-rows').innerText()).includes('name → value'),'composite index order visible');
    await page.locator('#apply-indexes').click();await idle();
    check(await page.evaluate(()=>state.schemaDirty),'index change stays in pending schema');
    await page.evaluate(()=>localDrafts.flush());
    check(await page.evaluate(()=>JSON.stringify(localDrafts.read(localDrafts.key('schema')).rawSchema.indexes)==='[["name","value"]]'),'index choices included in schema draft');
    await menu('preview');await idle();
    check((await page.locator('#schema-preview-content').innerText()).includes('name → value'),'preview describes index change');
    check((await page.locator('#schema-preview-status').innerText()).includes('校验通过'),'preview validates without committing');
    await page.screenshot({path:'output/playwright/schema-index-preview.png'});
    await page.locator('#close-schema-preview').click();await page.keyboard.press('Control+s');await idle();
    const saved=await (await page.request.get(origin+'/api/manage/tables/'+name+'/schema'+suffix)).json();
    check(JSON.stringify(saved.definition.indexes)==='[["name","value"]]','custom indexes persisted in server definition');
    await page.locator('#nav-schema').click();await page.locator('#schema-label').fill('已修改中文名');await page.keyboard.press('Control+s');await idle();
    const metadataSaved=await (await page.request.get(origin+'/api/manage/tables/'+name+'/schema'+suffix)).json();
    check(JSON.stringify(metadataSaved.definition.indexes)==='[["name","value"]]','ordinary metadata save preserves custom indexes');
    await page.locator('#nav-data').click();await page.locator('#sql-editor').fill(`SELECT * FROM "${name}" WHERE name='A' ORDER BY value`);
    await page.locator('#nav-schema').click();await menu('query-plan');await idle();
    check((await page.locator('#schema-preview-content').innerText()).includes('idx_c1_c2'),'query plan identifies selected composite index');
    await page.locator('#close-schema-preview').click();await menu('indexes');
    check(await page.locator('#index-mode').inputValue()==='custom','saved index mode restored when reopening editor');
    await page.locator('#index-rows button').filter({hasText:'删除索引'}).click();await page.locator('#apply-indexes').click();await idle();await page.keyboard.press('Control+s');await idle();
    const onlyKey=await (await page.request.get(origin+'/api/manage/tables/'+name+'/schema'+suffix)).json();
    check(onlyKey.definition.indexes.length===0,'explicit empty auxiliary list saved');
    await page.waitForFunction(()=>el('audit-warning').hidden);
    check(await page.locator('#audit-warning').isHidden(),'audit pending indicator clears after delivery');
    check(errors.length===0,'no browser exceptions');return {passed:checks.length,checks,errors};
  }finally{
    const current=await (await page.request.get(origin+'/api/manage/tables/'+name+'/schema'+suffix)).json();
    await page.request.delete(origin+'/api/manage/tables/'+name+'/schema'+suffix,{data:{confirm:name,version:current.version}});
  }
}
