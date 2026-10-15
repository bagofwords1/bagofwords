// Real Chromium: ⓘ color (infoClassName / data-bow-info-class), size
// (infoSize / data-bow-info-size) and hiding (info={false} / data-bow-info="off")
// on every source-backed component, legacy and themed runtimes; omitting them
// keeps today's ⓘ.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
const root=new URL('../../',import.meta.url);
const source=fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const VIZ='9f407b76-3037-4612-8c97-d3d9f23dba1b';
const code=`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  return (
    <div className="p-8 space-y-6">
      <ul id="custom-hidden" className="h-12 w-64" data-bow-viz="${VIZ}" data-bow-info="off"><li>Hidden</li></ul>
      <ul id="custom" className="h-24 w-64" data-bow-viz="${VIZ}" data-bow-info-class="text-red-500" data-bow-info-size="lg"><li>Tree</li></ul>
      <div id="hidden-section"><SectionCard title="Hidden" viz={viz} info={false}>x</SectionCard></div>
      <div id="hidden-kpi"><KPICard title="Hidden KPI" value="1" viz={viz} info={false} /></div>
      <div id="hidden-chart"><EChart viz={viz} info={false} option={{xAxis:{type:'category',data:['a']},yAxis:{type:'value'},series:[{type:'bar',data:[1]}]}} /></div>
      <div id="plain"><SectionCard title="Plain" viz={viz}>x</SectionCard></div>
      <div id="section"><SectionCard title="Section" viz={viz} infoClassName="text-red-500" infoSize="lg">x</SectionCard></div>
      <div id="kpi"><KPICard title="KPI" value="1" viz={viz} infoClassName="text-red-500" infoSize="sm" /></div>
      <div id="chart"><EChart viz={viz} option={{xAxis:{type:'category',data:['a']},yAxis:{type:'value'},series:[{type:'bar',data:[1]}]}} infoClassName="text-red-500" infoSize="lg" /></div>
      <div id="table"><DataTable viz={viz} info infoClassName="text-red-500" infoSize="lg" /></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true});
try{
 for(const runtime of [0,11]){
  const page=await browser.newPage();
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.pathname.startsWith('/libs/')){
    const file=new URL('public'+u.pathname,root);
    if(fs.existsSync(file))return route.fulfill({path:file.pathname});
   }
   return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
  });
  await page.goto('http://artifact-review.test/');
  await page.addScriptTag({content:'window.exports={};'+compiled});
  await page.evaluate(({code,VIZ,runtime})=>{
   window.msgs=[];addEventListener('message',e=>window.msgs.push(e.data));
   const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:runtime},visualizations:[{id:VIZ,title:'Albums',rows:[{Title:'A'}],columns:[{field:'Title'}]}]}});
   const f=document.createElement('iframe');f.style.width='900px';f.style.height='900px';f.srcdoc=html;document.body.appendChild(f);
  },{code,VIZ,runtime});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForFunction(()=>['plain','section','kpi','chart','table'].every(id=>document.querySelector('#'+id+' button[aria-label="Details"]')) && document.querySelector('[data-bow-ibtn]'));
  const errors=await page.evaluate(()=>window.msgs.filter(m=>m.type==='ARTIFACT_ERROR'));
  assert.deepEqual(errors,[],`runtime ${runtime}: renders`);
  // Tailwind's runtime emits a class's rule after first paint, and the ⓘ has
  // transition-colors, so let color transitions settle before measuring.
  await frame.waitForFunction(()=>document.getAnimations().length===0);
  const probe=sel=>frame.evaluate(sel=>{
   const b=document.querySelector(sel);const g=b.querySelector('svg');
   return {cls:b.getAttribute('class'),w:Math.round(b.getBoundingClientRect().width),glyph:Number(g.getAttribute('width')),color:getComputedStyle(b).color};
  },sel);
  const plain=await probe('#plain button[aria-label="Details"]');
  assert.equal(plain.w,20,`runtime ${runtime}: default size unchanged`);
  assert.equal(plain.glyph,15);
  assert.doesNotMatch(plain.cls,/red/);
  // This page's own text-red-500 (the themed runtime retunes the palette).
  const red=await frame.evaluate(()=>{const s=document.createElement('span');s.className='text-red-500';document.body.appendChild(s);const c=getComputedStyle(s).color;s.remove();return c});
  assert.notEqual(red,plain.color);
  for(const [id,w] of [['section',28],['kpi',16],['chart',28],['table',28]]){
   const p=await probe(`#${id} button[aria-label="Details"]`);
   assert.equal(p.w,w,`runtime ${runtime}: ${id} size`);
   assert.match(p.cls,/\btext-red-500\b/,`runtime ${runtime}: ${id} color class`);
   assert.doesNotMatch(p.cls,/text-ink-3|hover:text-/,`runtime ${runtime}: ${id} default color replaced`);
   assert.equal(p.color,red,`runtime ${runtime}: ${id} renders red`);
  }
  const custom=await probe('[data-bow-ibtn]');
  assert.equal(custom.w,28,`runtime ${runtime}: data-bow-info-size`);
  assert.equal(custom.color,red,`runtime ${runtime}: data-bow-info-class`);
  // Marker stays inside its owner's corner at the larger size.
  // info={false} / data-bow-info="off" hide it; the overlay keeps owner indexes in document order.
  for(const id of ['hidden-section','hidden-kpi','hidden-chart'])
   assert.equal(await frame.evaluate(id=>document.querySelectorAll('#'+id+' button[aria-label="Details"]').length,id),0,`runtime ${runtime}: ${id} hidden`);
  assert.equal(await frame.evaluate(()=>document.querySelectorAll('[data-bow-ibtn]').length),1,`runtime ${runtime}: only the visible custom owner gets a marker`);
  assert.equal(await frame.evaluate(()=>document.querySelector('[data-bow-ibtn]').getAttribute('data-bow-info-for')),'1',`runtime ${runtime}: marker points at #custom (index 1)`);
  const fits=await frame.evaluate(()=>{const o=document.querySelector('#custom').getBoundingClientRect(),m=document.querySelector('[data-bow-ibtn]').getBoundingClientRect();return m.right<=o.right&&m.left>=o.left});
  assert.ok(fits,`runtime ${runtime}: lg marker within owner`);
  await page.close();
 }
 // Geometry at lg (themed runtime, where overlay markers avoid each other):
 // nested owners sharing a corner, an owner overflowing the viewport edge,
 // the open panel below its marker, header room for the ⓘ, and an infoSize
 // that names an Object.prototype key.
 {
  const geom=`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  return (
    <div className="p-8 space-y-6">
      <div id="outer" className="h-24 w-72" data-bow-viz="${VIZ}" data-bow-info-size="lg"><div id="inner" className="h-12 w-72" data-bow-viz="${VIZ}" data-bow-info-size="lg">x</div></div>
      <div id="edge" style={{position:'fixed',top:160,right:-50,width:200,height:40}} data-bow-viz="${VIZ}" data-bow-info-size="lg">edge</div>
      <div id="padded" className="w-80"><SectionCard padding="sm" viz={viz} infoSize="lg" title="A deliberately long section title that has to wrap before the info icon">x</SectionCard></div>
      <div id="proto"><KPICard title="P" value="1" viz={viz} infoSize="constructor" /></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
  const page=await browser.newPage();
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
   return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
  });
  await page.goto('http://artifact-review.test/');
  await page.addScriptTag({content:'window.exports={};'+compiled});
  await page.evaluate(({geom,VIZ})=>{
   const html=exports.buildArtifactIframeHtml({code:geom,data:{runtime:{version:11},visualizations:[{id:VIZ,title:'Albums',rows:[{Title:'A'}],columns:[{field:'Title'}]}]}});
   const f=document.createElement('iframe');f.style.width='900px';f.style.height='700px';f.srcdoc=html;document.body.appendChild(f);
  },{geom,VIZ});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForFunction(()=>document.querySelectorAll('[data-bow-ibtn]').length===3 && document.querySelector('#proto button[aria-label="Details"]'));
  const g=await frame.evaluate(()=>{
   const box=el=>el.getBoundingClientRect();
   const m=[...document.querySelectorAll('[data-bow-ibtn]')].map(b=>({for:b.getAttribute('data-bow-info-for'),r:box(b)}));
   const by=i=>m.find(x=>x.for===String(i)).r;
   const title=document.querySelector('#padded h3, #padded h2');
   return {outer:by(0),inner:by(1),edge:by(2),vw:window.innerWidth,
    titleRight:box(title).right,iconLeft:box(document.querySelector('#padded button[aria-label="Details"]')).left,
    proto:Math.round(box(document.querySelector('#proto button[aria-label="Details"]')).width)};
  });
  const overlap=(a,b)=>a.left<b.right&&b.left<a.right&&a.top<b.bottom&&b.top<a.bottom;
  assert.equal(Math.round(g.outer.width),28);
  assert.ok(!overlap(g.outer,g.inner),'nested lg markers sharing a corner do not overlap');
  assert.ok(g.edge.right<=g.vw,`lg marker on an overflowing owner stays on screen (${g.edge.right} <= ${g.vw})`);
  assert.ok(g.titleRight<=g.iconLeft,`lg header room: title ends (${g.titleRight}) before the ⓘ (${g.iconLeft})`);
  assert.equal(g.proto,20,'infoSize naming a prototype key falls back to md');
  // The open panel starts below its own lg marker.
  await frame.click('[data-bow-ibtn][data-bow-info-for="2"]');
  await frame.waitForSelector('[data-bow-panel]');
  const panelTop=await frame.evaluate(()=>document.querySelector('[data-bow-panel]').getBoundingClientRect().top);
  assert.ok(panelTop>=g.edge.bottom,`panel (${panelTop}) opens below the lg marker (${g.edge.bottom})`);
  await page.close();
 }
 console.log('PASS: ⓘ infoClassName/infoSize/info={false} and data-bow-info-class/-size/off on SectionCard, KPICard, EChart, DataTable and custom markup; defaults unchanged (legacy + themed); lg geometry (marker spacing, viewport clamp, panel offset, header room); prototype-key sizes');
}finally{await browser.close()}
