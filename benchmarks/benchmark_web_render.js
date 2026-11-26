async (page) => {
  const origin=new URL(page.url()).origin;
  const forceFull=new URL(page.url()).searchParams.has('force_full');
  await page.goto(origin);
  await page.waitForFunction(()=>!state.busy && state.databases.length);
  await page.locator('#database-select').selectOption('lab.db');
  await page.waitForFunction(()=>!state.busy && state.records.length);
  await page.locator('#page-size').selectOption('500');
  await page.waitForFunction(()=>!state.busy && state.records.length===500);
  const metrics=await page.evaluate(async(forceFull)=>{
    const originalGrid=renderGrid, originalChart=drawChart, originalPatch=typeof patchGridRecords==='function'?patchGridRecords:null;
    let current;
    renderGrid=function(...args){const t=performance.now();try{return originalGrid(...args);}finally{if(current)current.grid_ms+=performance.now()-t;}};
    if(originalPatch)patchGridRecords=function(...args){const t=performance.now();try{return forceFull?false:originalPatch(...args);}finally{if(current)current.grid_ms+=performance.now()-t;}};
    drawChart=function(...args){const t=performance.now();try{return originalChart(...args);}finally{if(current)current.chart_ms+=performance.now()-t;}};
    const frames=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const samples={unchanged:[],single_cell:[]};
    try {
      for(const kind of Object.keys(samples)) {
        for(let i=-2;i<15;i++) {
          if(kind==='single_cell') await request(tablePath('sql'),'POST',{sql:`UPDATE samples SET v08=${1000+i} WHERE id=101`,confirm:'samples'});
          current={grid_ms:0,chart_ms:0,added_cells:0};
          const first=document.querySelector('#data-grid td');
          const observer=new MutationObserver(mutations=>{for(const m of mutations)for(const added of m.addedNodes)if(added.nodeType===1)current.added_cells+=(added.matches('td')?1:0)+added.querySelectorAll('td').length;});
          observer.observe(document.getElementById('data-grid'),{childList:true,subtree:true});
          const start=performance.now();await queryRows({silent:true});
          current.ready_ms=performance.now()-start;await frames();
          current.frame_ms=performance.now()-start;current.first_cell_reused=first===document.querySelector('#data-grid td');
          observer.disconnect();if(i>=0)samples[kind].push({...current});current=null;
        }
      }
      return samples;
    }finally{renderGrid=originalGrid;drawChart=originalChart;if(originalPatch)patchGridRecords=originalPatch;}
  },forceFull);
  const summarize=values=>{const v=[...values].sort((a,b)=>a-b);return {median:+v[Math.floor(v.length/2)].toFixed(2),p95:+v[Math.floor(.95*(v.length-1))].toFixed(2)};};
  const timings={};
  for(const [kind, samples] of Object.entries(metrics)) {
    timings[kind]=Object.fromEntries(['ready_ms','frame_ms','grid_ms','chart_ms','added_cells'].map(key=>[key,summarize(samples.map(s=>s[key]))]));
    timings[kind].all_first_cells_reused=samples.every(s=>s.first_cell_reused);
  }
  const restored=await page.request.post(origin+'/api/manage/tables/samples/sql?database=lab.db',{data:{sql:'UPDATE samples SET v08=101.08 WHERE id=101',confirm:'samples'}});
  if(!restored.ok())throw Error('fixture restoration failed');
  return {rows:500,columns:40,repeats:15,warmup:2,forceFull,scope:'browser query start to DOM ready / second animation frame; includes HTTP and JSON',browser:await page.evaluate(()=>navigator.userAgent),timings,samples:metrics};
}
