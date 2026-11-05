// Real Chromium, legacy + themed runtime:
//  - SDK labels drop their letter-spacing inside RTL content (Hebrew has no
//    capitals; tracking pulls the letters apart) and keep it in LTR content,
//    including an LTR island inside RTL text;
//  - the provenance ⓘ (inline and detached overlay) is quiet at rest, comes up
//    when its own card/owner is hovered (not its neighbours'), and stays up
//    while its panel is open.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';
const root=new URL('../../',import.meta.url);
const source=fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const VIZ='5b0c8a51-77d2-4f0e-9a1b-2c4d6e8f0a13';
const app=`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  return (
    <div className="p-8 grid grid-cols-2 gap-6">
      <div dir="rtl" id="he"><KPICard title="הכנסות 2025" value="451" viz={viz} /><p>מק״ט <span id="island" dir="ltr" className="tracking-widest">SKU-12</span></p></div>
      <div dir="ltr" id="en"><KPICard title="Revenue 2025" value="451" viz={viz} /></div>
      <div id="custom" className="h-24 w-64" data-bow-viz="${VIZ}">custom</div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true, executablePath:CHROME_PATH});
try{
 for(const runtime of [0,11]){
  const page=await browser.newPage({viewport:{width:1000,height:700}});
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
   return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
  });
  await page.goto('http://artifact-review.test/');
  await page.addScriptTag({content:'window.exports={};'+compiled});
  await page.evaluate(({code,VIZ,runtime})=>{
   const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:runtime},visualizations:[{id:VIZ,title:'Revenue',rows:[{Total:451}],columns:[{field:'Total'}]}]}});
   const f=document.createElement('iframe');f.style.width='980px';f.style.height='680px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
  },{code:app,VIZ,runtime});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForFunction(()=>document.querySelector('#he button[aria-label="Details"]')&&document.querySelector('#en button[aria-label="Details"]')&&document.querySelector('[data-bow-ibtn]'));
  const label=`runtime ${runtime}`;

  // Letter-spacing of the KPI title (the element whose text is the title).
  const spacing=await frame.evaluate(()=>{
   const find=(id,text)=>[...document.querySelectorAll(`#${id} *`)].find(e=>e.childElementCount===0&&e.textContent.trim()===text);
   return {he:getComputedStyle(find('he','הכנסות 2025')).letterSpacing,en:getComputedStyle(find('en','Revenue 2025')).letterSpacing,island:getComputedStyle(document.getElementById('island')).letterSpacing};
  });
  assert.equal(spacing.he,'normal',`${label}: RTL KPI label has no letter-spacing`);
  assert.notEqual(spacing.en,'normal',`${label}: LTR KPI label keeps its tracking`);
  assert.notEqual(spacing.island,'normal',`${label}: an LTR island inside RTL keeps its tracking`);

  const opacity=sel=>frame.evaluate(sel=>Number(getComputedStyle(document.querySelector(sel)).opacity),sel);
  const settle=()=>page.waitForTimeout(250); // the 150ms opacity transition
  const he='#he button[aria-label="Details"]', en='#en button[aria-label="Details"]', mark='[data-bow-ibtn]';
  await page.mouse.move(990,690); await settle();
  for(const sel of [he,en,mark]) assert.ok(await opacity(sel)<0.6,`${label}: ${sel} is quiet at rest`);

  // Hovering one card raises its own ⓘ only.
  await frame.hover('#en > *'); await settle();
  assert.equal(await opacity(en),1,`${label}: hovered card's ⓘ is fully visible`);
  assert.ok(await opacity(he)<0.6,`${label}: the neighbouring card's ⓘ stays quiet`);
  // A detached overlay marker comes up when its owner is hovered.
  await frame.hover('#custom'); await settle();
  assert.equal(await opacity(mark),1,`${label}: hovering a data-bow-viz owner raises its marker`);
  assert.ok(await opacity(en)<0.6,`${label}: and the card left behind goes quiet again`);

  // Open panels keep their button up after the pointer leaves.
  for(const sel of [he,mark]){
   await frame.click(sel); await page.mouse.move(990,690); await settle();
   assert.equal(await frame.getAttribute(sel,'aria-expanded'),'true',`${label}: ${sel} reports its open panel`);
   assert.equal(await opacity(sel),1,`${label}: ${sel} stays visible while open`);
   await frame.press('body','Escape').catch(()=>{}); await page.mouse.click(990,690); await settle();
  }
  await page.close();
 }
 console.log('PASS: RTL SDK labels lose letter-spacing (LTR labels and LTR islands keep it); the ⓘ is quiet until its own card/owner is hovered or its panel is open (inline + overlay, legacy + themed)');
}finally{await browser.close()}
