// Real Chromium: a TrendChart / RankChart whose viz is no longer on the page
// (vizById returns null where the page is viewed: its query was deleted
// through DELETE /api/queries/{id}, which leaves the page's references) and
// that has no rows of its own tells the viewer the chart's data is no longer
// available. The developer instruction 'needs viz={vizById("…")}' is for the
// page's author and stays only for a chart that names no viz at all.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';
const root=new URL('../../',import.meta.url);
const compile=f=>ts.transpileModule(fs.readFileSync(new URL(f,root),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const iframeJs=compile('utils/artifactIframe.ts');
const VIZ='1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d';
const GONE='9f8e7d6c-5b4a-4f3e-8d2c-1b0a9f8e7d6c';
const rows=[{month:'2025-01',rep:'Rep A',revenue:12},{month:'2025-02',rep:'Rep B',revenue:9}];
const app=`<script type="text/babel">
function App() {
  const gone = vizById("${GONE}");
  return (
    <div className="p-4 space-y-4">
      <div id="trend"><TrendChart viz={gone} x="month" y="revenue" /></div>
      <div id="rank"><RankChart viz={vizById("${GONE}")} label="rep" value="revenue" /></div>
      <div id="he"><RankChart viz={gone} label="נציג" value="סכום" lang="he" /></div>
      <div id="noviz"><TrendChart x="month" y="revenue" /></div>
      <div id="live"><TrendChart viz={vizById("${VIZ}")} x="month" y="revenue" /></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true, executablePath:CHROME_PATH});
try{
 const page=await browser.newPage({viewport:{width:390,height:1200}});
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
  return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
 });
 await page.goto('http://artifact-review.test/');
 await page.addScriptTag({content:'window.exports={};'+iframeJs});
 await page.evaluate(({code,VIZ,rows})=>{
  const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[{id:VIZ,title:'Revenue',rows,columns:[{field:'month'},{field:'rep'},{field:'revenue'}]}]}});
  const f=document.createElement('iframe');f.style.width='390px';f.style.height='1180px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
 },{code:app,VIZ,rows});
 const frame=page.frames().find(f=>f.parentFrame());
 await frame.waitForFunction(()=>window.__bowCharts&&document.querySelectorAll('[data-bow-chart]').length===5,null,{timeout:30000});
 await frame.waitForTimeout(600);

 const dom=await frame.evaluate(()=>{
  const at=id=>{const el=document.getElementById(id);return {
   canvas:el.querySelectorAll('canvas').length, alert:(el.querySelector('[role=alert]')||{}).textContent||null,
   text:el.textContent.trim()};};
  return {trend:at('trend'),rank:at('rank'),he:at('he'),noviz:at('noviz'),live:at('live')};
 });
 for(const id of ['trend','rank']){
  assert.equal(dom[id].alert,null,id+': a viz that is no longer on the page is not an author error');
  assert.equal(dom[id].canvas,0,id+': nothing to draw');
  assert.match(dom[id].text,/This chart's data is no longer available/,id+': the viewer is told the data is gone');
  assert.doesNotMatch(dom[id].text,/vizById/,id+': no developer instruction');
 }
 assert.equal(dom.he.alert,null);
 assert.match(dom.he.text,/הנתונים של התרשים הזה אינם זמינים עוד/,'the message follows the chart language');
 assert.match(dom.noviz.alert||'',/TrendChart needs viz=\{vizById\("…"\)\} or rows/,'a chart that names no viz keeps the author error');
 assert.deepEqual({canvas:dom.live.canvas,alert:dom.live.alert},{canvas:1,alert:null},'a chart with a live viz draws');
 console.log('PASS: a chart whose viz is no longer on the page says its data is no longer available');
}finally{await browser.close()}
