async(page)=>{
  await page.reload();const idle=()=>page.waitForFunction(()=>typeof state!=='undefined'&&!state.busy&&localDrafts.ready&&state.databases.length);await idle();
  if(!await page.evaluate(()=>state.dataDir.includes('workflow-lab')))throw Error('isolated lab required');
  const value=await page.evaluate(()=>JSON.parse(localStorage.getItem(csvImports.key)));
  const session=await(await page.request.get(new URL(page.url()).origin+'/api/manage/database-uploads/'+value.id)).json();
  await page.locator('#database-select').selectOption(session.database);await idle();
  await page.locator('#table-list').click({button:'right'});await page.getByRole('menuitem',{name:'CSV 流式导入',exact:true}).click();await idle();
  const closeVisible=await page.locator('#csv-close').evaluate(e=>{const a=e.getBoundingClientRect(),b=document.querySelector('#csv-dialog').getBoundingClientRect();return a.bottom<=b.bottom&&a.top>=b.top;});
  if(!closeVisible)throw Error('Close button clipped');
  await page.screenshot({path:'output/playwright/csv-preview.png'});
  await page.setViewportSize({width:430,height:900});await page.screenshot({path:'output/playwright/csv-mobile.png'});await page.setViewportSize({width:1280,height:720});
  await page.route('**/api/manage/tables/devices/sql?*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'模拟刷新失败'})}));
  try{
    await page.locator('#csv-apply').click();await page.waitForFunction(()=>!state.busy&&!csvImports.active);
    const message=await page.locator('#csv-status').innerText();
    if(!message.includes('未重复写入')||!message.includes('页面刷新失败'))throw Error(message);
  }finally{await page.unrouteAll();}
  await page.locator('#csv-apply').click();await page.waitForFunction(()=>!state.busy&&!csvImports.active);
  await page.screenshot({path:'output/playwright/csv-import-complete.png'});
  return {closeVisible,committedStatusSurvivesRefreshFailure:true,replayStillSafe:true};
}
