// Real Chromium: the ⓘ keeps its 20px box and visible glyph when the artifact
// styles `button` / `svg` generically (a customer dashboard's `#root button
// { padding; border }` squeezed the glyph to nothing inside a padded box).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
const root=new URL('../../',import.meta.url);
const source=fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const VIZ='9f407b76-3037-4612-8c97-d3d9f23dba1b';
const HOSTILE='#root button{display:block;padding:6px 16px;border:1px solid #e5e7eb;border-radius:10px;background:#fff;min-width:36px;min-height:26px;width:auto;height:auto}'
 +' button{padding:8px 20px;min-width:40px;margin:4px}'
 +' svg{width:100%;height:auto}';
const app=hostile=>`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  return (
    <div className="p-8 space-y-6" dir="rtl">
      ${hostile?`<style>{${JSON.stringify(HOSTILE)}}</style>`:''}
      <div id="kpi"><KPICard title="דירות שנמכרו" value="156" viz={viz} /></div>
      <div id="section"><SectionCard title="מצב הדירות" viz={viz}>x</SectionCard></div>
      <div id="custom" className="h-24 w-64" data-bow-viz="${VIZ}">custom</div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true});
try{
 for(const runtime of [0,11]) for(const hostile of [false,true]){
  const page=await browser.newPage();
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
   return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
  });
  await page.goto('http://artifact-review.test/');
  await page.addScriptTag({content:'window.exports={};'+compiled});
  await page.evaluate(({code,VIZ,runtime})=>{
   const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:runtime},visualizations:[{id:VIZ,title:'Albums',rows:[{Title:'A'}],columns:[{field:'Title'}]}]}});
   const f=document.createElement('iframe');f.style.width='900px';f.style.height='700px';f.srcdoc=html;document.body.appendChild(f);
  },{code:app(hostile),VIZ,runtime});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForFunction(()=>document.querySelector('#kpi button[aria-label="Details"]')&&document.querySelector('#section button[aria-label="Details"]')&&document.querySelector('[data-bow-ibtn]'));
  const label=`runtime ${runtime}${hostile?' + generic button/svg CSS':''}`;
  for(const [sel,glyph] of [['#kpi button[aria-label="Details"]',15],['#section button[aria-label="Details"]',15],['[data-bow-ibtn]',14]]){
   const m=await frame.evaluate(sel=>{const b=document.querySelector(sel),r=b.getBoundingClientRect(),g=b.querySelector('svg').getBoundingClientRect(),cs=getComputedStyle(b);return {w:Math.round(r.width),h:Math.round(r.height),gw:Math.round(g.width),gh:Math.round(g.height),pad:cs.paddingLeft,dx:Math.round((g.left-r.left)*2)/2,dy:Math.round((g.top-r.top)*2)/2}},sel);
   const c=(20-glyph)/2;
   assert.deepEqual(m,{w:20,h:20,gw:glyph,gh:glyph,pad:'0px',dx:c,dy:c},`${label}: ${sel} (box, glyph, centered)`);
  }
  if(hostile){
   // The artifact's own buttons still get its rule: only the ⓘ is pinned.
   const own=await frame.evaluate(()=>{const b=document.createElement('button');b.textContent='Reset';document.getElementById('root').firstElementChild.appendChild(b);const p=getComputedStyle(b).paddingLeft;b.remove();return p});
   assert.equal(own,'16px',`${label}: artifact buttons keep their own styling`);
  }
  await page.close();
 }
 console.log('PASS: ⓘ keeps a 20px box and a 15/14px glyph under generic button/svg CSS (inline + overlay, legacy + themed, RTL); artifact buttons unaffected');
}finally{await browser.close()}
