// Real Chromium: ⓘ color (infoClassName / data-bow-info-class) and size
// (infoSize / data-bow-info-size) on every source-backed component, legacy and
// themed runtimes; omitting them keeps today's ⓘ.
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
      <ul id="custom" className="h-24 w-64" data-bow-viz="${VIZ}" data-bow-info-class="text-red-500" data-bow-info-size="lg"><li>Tree</li></ul>
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
  const fits=await frame.evaluate(()=>{const o=document.querySelector('#custom').getBoundingClientRect(),m=document.querySelector('[data-bow-ibtn]').getBoundingClientRect();return m.right<=o.right&&m.left>=o.left});
  assert.ok(fits,`runtime ${runtime}: lg marker within owner`);
  await page.close();
 }
 console.log('PASS: ⓘ infoClassName/infoSize and data-bow-info-class/-size on SectionCard, KPICard, EChart, DataTable and custom markup; defaults unchanged (legacy + themed)');
}finally{await browser.close()}
