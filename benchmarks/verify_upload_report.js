async(page)=>{
  await page.goto('file:///D:/codex/rtdb/output/rtdb-resumable-upload-20261115.html');
  await page.setViewportSize({width:1280,height:900});
  await page.evaluate(()=>document.fonts.ready);
  const images=await page.locator('img').evaluateAll(elements=>elements.every(e=>e.complete&&e.naturalWidth>0));
  if(!images)throw Error('Embedded images failed to load');
  const text=await page.locator('main').innerText();
  if(!text.includes('249项')||!text.includes('771.2 ms')||!text.includes('4443.9 ms'))throw Error('Measured values missing');
  await page.screenshot({path:'output/playwright/upload-report-desktop.png'});
  await page.setViewportSize({width:430,height:900});
  const fits=await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth);
  if(!fits)throw Error('Report horizontal overflow');
  await page.screenshot({path:'output/playwright/upload-report-mobile.png'});
  await page.setViewportSize({width:1280,height:720});
  return {title:await page.title(),embeddedImages:images,narrowLayout:fits,measurementsVerified:true};
}
