// Real Chromium: the data-bow-viz overlay ⓘ does not sit on a short owner's
// text (a customer's `<label data-bow-viz>פרויקט</label>` had it drawn over the
// word). When the inner top-end corner would cover the owner's text, free space
// beside, above or below is used — free of elements and of the surrounding
// sentence's text; owners whose corner is free keep it.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
const root=new URL('../../',import.meta.url);
const source=fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const VIZ='9f407b76-3037-4612-8c97-d3d9f23dba1b';
const sel='<select className="border rounded-lg px-3 py-2"><option>All projects</option></select>';
const app=dir=>`<script type="text/babel">
document.documentElement.setAttribute('dir','${dir}');
function App() {
  return (
    <div className="p-6 space-y-8 text-sm">
      <p>Caption above the first row</p>
      <div className="flex items-center gap-10"><label id="roomy" data-bow-viz="${VIZ}">Project</label>${sel}</div>
      <div className="flex items-center gap-2"><label id="tight" data-bow-viz="${VIZ}">Project</label>${sel}<button className="px-4 py-2 border rounded-lg">Reset</button></div>
      <div id="card" className="h-24 w-72 rounded-xl border p-4" data-bow-viz="${VIZ}"><p>Card title</p></div>
      <p id="para">Sales for <span id="q3" data-bow-viz="${VIZ}">Q3</span> were up strongly this year</p>
      <div id="bar" className="w-full h-7 border flex items-center px-2" data-bow-viz="${VIZ}">Revenue by month</div>
      <div id="wrapbar" className="w-full h-7 border px-2" data-bow-viz="${VIZ}"><div className="flex items-center h-full">Revenue by month</div></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true});
try{
 for(const runtime of [0,11]) for(const dir of ['rtl','ltr']){
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
   const f=document.createElement('iframe');f.style.width='800px';f.style.height='500px';f.srcdoc=html;document.body.appendChild(f);
  },{code:app(dir),VIZ,runtime});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForFunction(()=>document.querySelectorAll('[data-bow-ibtn]').length===6);
  await page.waitForTimeout(300);
  const g=await frame.evaluate(()=>{
   const box=r=>({left:r.left,right:r.right,top:r.top,bottom:r.bottom});
   const owners=[...document.querySelectorAll('[data-bow-viz]')];
   const marker=id=>{const i=owners.indexOf(document.getElementById(id));return box(document.querySelector(`[data-bow-ibtn][data-bow-info-for="${i}"]`).getBoundingClientRect())};
   const text=id=>{const r=document.createRange();r.selectNodeContents(document.getElementById(id));return box(r.getBoundingClientRect())};
   const busy=[...document.querySelectorAll('select, button:not([aria-label="Details"]), p')].map(e=>box(e.getBoundingClientRect()));
   const textRects=id=>{const r=document.createRange();r.selectNodeContents(document.getElementById(id));return [...r.getClientRects()].filter(x=>x.width&&x.height).map(box)};
   return {roomy:marker('roomy'),roomyText:text('roomy'),tight:marker('tight'),tightText:text('tight'),card:marker('card'),cardBox:box(document.getElementById('card').getBoundingClientRect()),
    q3:marker('q3'),paraText:textRects('para'),bar:marker('bar'),barBox:box(document.getElementById('bar').getBoundingClientRect()),
    wrapbar:marker('wrapbar'),wrapbarBox:box(document.getElementById('wrapbar').getBoundingClientRect()),busy};
  });
  const overlap=(a,b)=>a.left<b.right&&b.left<a.right&&a.top<b.bottom&&b.top<a.bottom;
  const label=`runtime ${runtime} ${dir}`;
  for(const id of ['roomy','tight']){
   assert.ok(!overlap(g[id],g[id+'Text']),`${label}: ⓘ does not cover the ${id} label's text`);
   for(const b of g.busy) assert.ok(!overlap(g[id],b),`${label}: ⓘ of the ${id} label does not cover a neighbor`);
  }
  // With room beside the label, the ⓘ sits next to it, on its line.
  assert.ok(g.roomy.top<g.roomyText.bottom&&g.roomy.bottom>g.roomyText.top,`${label}: roomy ⓘ is beside its label`);
  // A card keeps today's inner top-end corner.
  const cornerLeft=dir==='rtl'?g.cardBox.left+4:g.cardBox.right-24;
  assert.equal(Math.round(g.card.left),Math.round(cornerLeft),`${label}: card ⓘ stays at its inner corner (x)`);
  assert.equal(Math.round(g.card.top),Math.round(g.cardBox.top+6),`${label}: card ⓘ stays at its inner corner (y)`);
  // An inline owner inside a sentence: the ⓘ covers none of the sentence's words
  // (the parent's own text, which elementsFromPoint can't see).
  for(const r of g.paraText) assert.ok(!overlap(g.q3,r),`${label}: Q3 ⓘ covers no word of its sentence`);
  // A short but wide owner whose inner corner is empty keeps that corner.
  const barLeft=dir==='rtl'?g.barBox.left+4:g.barBox.right-24;
  assert.equal(Math.round(g.bar.left),Math.round(barLeft),`${label}: short strip ⓘ stays at its inner corner (x)`);
  assert.equal(Math.round(g.bar.top),Math.round(g.barBox.top+6),`${label}: short strip ⓘ stays at its inner corner (y)`);
  // Same strip with its text in an inner wrapper: the wrapper's box is not text.
  const wrapLeft=dir==='rtl'?g.wrapbarBox.left+4:g.wrapbarBox.right-24;
  assert.equal(Math.round(g.wrapbar.left),Math.round(wrapLeft),`${label}: wrapped strip ⓘ stays at its inner corner (x)`);
  assert.equal(Math.round(g.wrapbar.top),Math.round(g.wrapbarBox.top+6),`${label}: wrapped strip ⓘ stays at its inner corner (y)`);
  // The open panel never covers the (possibly moved) ⓘ that opened it.
  for(const id of ['tight','q3']){
   const sel=await frame.evaluate(id=>{const i=[...document.querySelectorAll('[data-bow-viz]')].indexOf(document.getElementById(id));return `[data-bow-ibtn][data-bow-info-for="${i}"]`},id);
   await frame.click(sel);
   await frame.waitForSelector('[data-bow-panel]');
   const pm=await frame.evaluate(sel=>{const b=x=>{const r=x.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom}};return {p:b(document.querySelector('[data-bow-panel]')),m:b(document.querySelector(sel))}},sel);
   assert.ok(!overlap(pm.p,pm.m),`${label}: open panel leaves the ${id} ⓘ visible`);
   await frame.press('body','Escape');
   await frame.waitForFunction(()=>!document.querySelector('[data-bow-panel]'));
  }
  await page.close();
 }
 console.log('PASS: overlay ⓘ leaves a small owner\'s text, its neighbors and its sentence uncovered (beside when roomy, else above/below); owners whose corner is free (cards, short strips) keep it (legacy + themed, RTL + LTR)');
}finally{await browser.close()}
