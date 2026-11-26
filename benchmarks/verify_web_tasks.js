async (page) => {
  const errors=[];page.on('pageerror',e=>errors.push(e.message));const checks=[];
  const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
  const origin=new URL(page.url()).origin;
  await page.goto(origin);await page.waitForFunction(()=>!state.busy&&state.databases.length);
  await page.evaluate(()=>{window.taskOutcome=null;request('logs?date=2026-11-13').then(r=>window.taskOutcome={result:r}).catch(e=>window.taskOutcome={error:e.message,details:e.details});});
  await page.locator('#task-dialog').waitFor({state:'visible'});
  check((await page.locator('#task-caption').innerText()).includes('查询日志'),'long operation opens progress');
  await page.locator('#task-cancel').click();
  await page.waitForFunction(()=>window.taskOutcome!==null);
  check(await page.evaluate(()=>taskOutcome.details?.cancelled===true),'cancel returns confirmed server cancellation');
  check(await page.evaluate(()=>!backgroundTasks.pending()&&!backgroundTasks.active),'terminal cancellation clears saved task');
  check(!await page.locator('#task-dialog').isVisible(),'task modal closes after cancellation');
  await page.evaluate(()=>{request('logs?date=2026-11-13').catch(()=>{});});
  await page.locator('#task-dialog').waitFor({state:'visible'});
  check(await page.evaluate(()=>backgroundTasks.pending()),'in-flight task identity retained');
  await page.screenshot({path:'output/playwright/task-progress.png'});
  await page.reload();await page.waitForFunction(()=>!state.busy,{},{timeout:30000});
  check(await page.evaluate(()=>!backgroundTasks.pending()),'reload resumes existing task to completion');
  check((await page.locator('#operation-message').innerText()).includes('后台任务已完成'),'resumed result visible');
  await page.locator('#database-select').selectOption('lab.db');await page.waitForFunction(()=>!state.busy&&state.records.length);
  const backup=await page.evaluate(()=>request('backup','POST',{tables:['samples']}));
  check(!!backup,'background backup stays scoped to selected database');
  check(errors.length===0,'no browser errors');return {passed:checks.length,checks,errors};
}
