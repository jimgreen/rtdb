async(page)=>{
  await page.goto('file:///D:/codex/rtdb/output/rtdb-efile-streaming-20261115.html');
  await page.setViewportSize({width:1280,height:900});await page.evaluate(()=>document.fonts.ready);
  const images=await page.locator('img').evaluateAll(items=>items.length===3&&items.every(e=>e.complete&&e.naturalWidth>0));
  if(!images)throw Error('Embedded image missing');
  const text=await page.locator('main').innerText();
  if(!text.includes('273项完整回归')||!text.includes('50,000')||!text.includes('不含HTTP'))throw Error('Report evidence incomplete');
  await page.screenshot({path:'output/playwright/efile-report-desktop.png'});
  await page.setViewportSize({width:430,height:900});
  const fits=await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth);
  if(!fits)throw Error('Report horizontal overflow');
  await page.screenshot({path:'output/playwright/efile-report-mobile.png'});
  await page.setViewportSize({width:1280,height:720});
  return {title:await page.title(),embeddedImages:images,narrowLayout:fits};
}
