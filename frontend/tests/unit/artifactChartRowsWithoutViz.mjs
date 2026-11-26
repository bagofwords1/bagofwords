// Real Chromium: a TrendChart / RankChart given `rows` but no viz draws from
// the rows. Pages saved with that shape (viz omitted, or a viz that resolves
// to null where the page is viewed) showed 'needs viz={vizById("…")}' instead
// of the chart. Without a viz there is nothing to explain, so no ⓘ. A chart
// with neither viz nor rows still shows the error.
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
const rows=[{month:'2025-01',rep:'Rep A',revenue:12},{month:'2025-02',rep:'Rep A',revenue:15},{month:'2025-03',rep:'Rep A',revenue:11},
 {month:'2025-01',rep:'Rep B',revenue:8},{month:'2025-02',rep:'Rep B',revenue:9},{month:'2025-03',rep:'Rep B',revenue:14}];
const heRows=[{'נציג':'א',sum:5},{'נציג':'ב',sum:9},{'נציג':'ג',sum:2}];
const app=`<script type="text/babel">
const ROWS = ${JSON.stringify(rows)};
const HE = ${JSON.stringify(heRows)};
function App() {
  const gone = vizById("${GONE}");
  return (
    <div className="p-4 space-y-4">
      <div id="trend"><TrendChart rows={ROWS} x="month" y="revenue" series="rep" /></div>
      <div id="rank"><RankChart rows={ROWS} label="rep" value="revenue" additive /></div>
      <div id="nullviz"><TrendChart viz={gone} rows={ROWS} x="month" y="revenue" series="rep" /></div>
      <div id="he"><RankChart rows={HE} label="נציג" value="sum" lang="he" /></div>
      <div id="nothing"><TrendChart x="month" y="revenue" /></div>
      <div id="withviz"><RankChart viz={vizById("${VIZ}")} label="rep" value="revenue" additive /></div>
      <div id="vizbroken"><RankChart viz={vizById("${VIZ}")} value="revenue" /></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true, executablePath:CHROME_PATH});
try{
 const page=await browser.newPage({viewport:{width:390,height:1400}});
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
  return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
 });
 await page.goto('http://artifact-review.test/');
 await page.addScriptTag({content:'window.exports={};'+iframeJs});
 await page.evaluate(({code,VIZ,rows})=>{
  const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[{id:VIZ,title:'Revenue',rows,columns:[{field:'month'},{field:'rep'},{field:'revenue'}]}]}});
  const f=document.createElement('iframe');f.style.width='390px';f.style.height='1380px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
 },{code:app,VIZ,rows});
 const frame=page.frames().find(f=>f.parentFrame());
 await frame.waitForFunction(()=>window.__bowCharts&&document.querySelectorAll('[data-bow-chart]').length===7,null,{timeout:30000});
 await frame.waitForTimeout(600);

 // Builders: rows alone are enough; neither viz nor rows is still an error.
 const b=await frame.evaluate(({rows})=>{
  const B=window.__bowCharts, theme=window.bowTheme;
  const t=B.buildTrend({rows,x:'month',y:'revenue',series:'rep'},theme,'en');
  const k=B.buildRank({rows,label:'rep',value:'revenue',additive:true},theme,'en',false);
  const miss=B.buildTrend({rows,x:'month',y:'sales'},theme,'en');
  const none=B.buildTrend({x:'month',y:'revenue'},theme,'en');
  const noneRank=B.buildRank({label:'rep',value:'revenue'},theme,'en',false);
  return {
   trend:t.error||{cats:t.option.xAxis.data,series:t.option.series.map(s=>s.name),a:t.option.series[0].data},
   rank:k.error||{bars:k.option.yAxis.data,values:k.option.series[0].data.map(d=>d.value)},
   miss:miss.error||null, none:none.error||null, noneRank:noneRank.error||null,
   empty:!!B.buildRank({rows:[],label:'rep',value:'revenue'},theme,'en',false).empty,
  };
 },{rows});
 assert.deepEqual(b.trend,{cats:['Jan 2025','Feb 2025','Mar 2025'],series:['Rep A','Rep B'],a:[12,15,11]},'TrendChart draws from rows without a viz');
 assert.deepEqual(b.rank,{bars:['Rep A','Rep B'],values:[38,31]},'RankChart draws from rows without a viz');
 assert.match(b.miss||'',/no field named sales\. Available fields: month, rep, revenue/,'fields are checked against the rows');
 assert.match(b.none||'',/TrendChart needs viz=\{vizById\("…"\)\} or rows/,'neither viz nor rows: TrendChart error');
 assert.match(b.noneRank||'',/RankChart needs viz=\{vizById\("…"\)\} or rows/,'neither viz nor rows: RankChart error');
 assert.equal(b.empty,true,'empty rows without a viz: No data');

 const dom=await frame.evaluate(()=>{
  const at=id=>{const el=document.getElementById(id);return {
   canvas:el.querySelectorAll('canvas').length, alert:(el.querySelector('[role=alert]')||{}).textContent||null,
   info:el.querySelectorAll('button[aria-label="Details"]').length};};
  return {trend:at('trend'),rank:at('rank'),nullviz:at('nullviz'),he:at('he'),nothing:at('nothing'),withviz:at('withviz'),
   heDir:document.querySelector('#he [data-bow-chart]').getAttribute('dir'),
   // The page check reads each warning box's named message from the DOM.
   warnings:Array.from(document.querySelectorAll('[data-bow-chart-warning]')).map(e=>[e.closest('[id]').id,e.getAttribute('data-bow-chart-warning'),e.textContent,e.getAttribute('data-bow-chart-viz')])};
 });
 assert.deepEqual(dom.trend,{canvas:1,alert:null,info:0},'TrendChart with rows and no viz: a chart, no ⓘ');
 assert.deepEqual(dom.rank,{canvas:1,alert:null,info:0},'RankChart with rows and no viz: a chart, no ⓘ');
 assert.deepEqual(dom.nullviz,{canvas:1,alert:null,info:0},'a viz variable that resolves to null, with rows: a chart, no ⓘ');
 assert.deepEqual(dom.he,{canvas:1,alert:null,info:0},'Hebrew ranking from rows: a chart');
 assert.equal(dom.heDir,'rtl','the Hebrew ranking is mirrored');
 assert.equal(dom.nothing.canvas,0); assert.match(dom.nothing.alert||'',/TrendChart needs viz/,'neither viz nor rows: the error is kept');
 assert.deepEqual(dom.withviz,{canvas:1,alert:null,info:1},'a chart with a viz keeps its ⓘ');
 assert.deepEqual(dom.warnings.map(w=>w[0]),['nothing','vizbroken'],'only the boxes carry a page-check warning; drawn charts carry none');
 assert.match(dom.warnings[0][1],/^TrendChart needs viz=\{vizById\("…"\)\} or rows/,'a chart with no viz: its message');
 assert.match(dom.warnings[1][1],/^RankChart \(Revenue\) needs label and value/,'a chart with a viz is named by its title');
 assert.match(dom.warnings[1][2],/^RankChart needs label and value/,'the box viewers see keeps the plain message');
 assert.deepEqual(dom.warnings.map(w=>w[3]),[null,VIZ],'the viz id rides along: titles repeat across vizzes');
 console.log('PASS: TrendChart/RankChart draw from rows without a viz; the error stays only without both');
}finally{await browser.close()}
