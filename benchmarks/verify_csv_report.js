async(page)=>{
  await page.goto('file:///D:/codex/rtdb/output/rtdb-csv-streaming-20261115.html');
  await page.setViewportSize({width:1280,height:900});
  await page.evaluate(()=>document.fonts.ready);
  const images=await page.locator('img').evaluateAll(elements=>elements.length===3&&elements.every(e=>e.complete&&e.naturalWidth>0));
  if(!images)throw Error('Embedded images failed to load');
  const text=await page.locator('main').innerText();
  if(!text.includes('260项')||!text.includes('13039.3 ms')||!text.includes('10585.1 ms'))throw Error('Measured values missing');
  await page.screenshot({path:'output/playwright/csv-report-desktop.png'});
  await page.setViewportSize({width:430,height:900});
  const fits=await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth);
  if(!fits)throw Error('Report horizontal overflow');
  await page.screenshot({path:'output/playwright/csv-report-mobile.png'});
  await page.setViewportSize({width:1280,height:720});
  return {title:await page.title(),embeddedImages:images,narrowLayout:fits,measurementsVerified:true};
}
