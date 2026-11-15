// Paired renderer comparison. No database writes; same loaded 500 x 40 page.
async(page)=>{
  const origin=new URL(page.url()).origin;
  await page.goto(origin);await page.waitForFunction(()=>!state.busy&&state.databases.length);
  if(!await page.evaluate(()=>state.dataDir.includes('browser-render-lab')))throw Error('fixture only');
  await page.locator('#database-select').selectOption('lab.sqlite3');await page.waitForFunction(()=>!state.busy&&state.records.length);
  await page.locator('#page-size').selectOption('500');await page.waitForFunction(()=>!state.busy&&state.records.length===500);
  const samples=await page.evaluate(async()=>{
    const samples={full:[],virtual:[]},frames=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    try{
      for(let i=-2;i<15;i++)for(const mode of i%2?['full','virtual']:['virtual','full']){
        history.replaceState(null,'',mode==='full'?'?full_grid=1':'/');el('data-grid').scrollTop=0;el('data-grid').scrollLeft=0;
        await frames();const start=performance.now();renderGrid();const render_ms=performance.now()-start;await frames();
        const sample={render_ms,frame_ms:performance.now()-start,cells:document.querySelectorAll('#data-grid td.value').length};
        if(i>=0)samples[mode].push(sample);
      }
    }finally{history.replaceState(null,'','/');renderGrid();}
    return samples;
  });
  const summary=values=>{values.sort((a,b)=>a-b);return {median_ms:+values[Math.floor(values.length/2)].toFixed(3),p95_ms:+values[Math.ceil(values.length*.95)-1].toFixed(3)};};
  return {rows:500,columns:40,repeats:15,warmups:2,scope:'renderGrid start through synchronous DOM update / second animation frame; excludes HTTP, SQL and parsing',
    viewport:page.viewportSize(),timings:Object.fromEntries(Object.entries(samples).map(([mode,items])=>[mode,{render:summary(items.map(s=>s.render_ms)),frame:summary(items.map(s=>s.frame_ms)),cells:items[0].cells}])),samples};
}
