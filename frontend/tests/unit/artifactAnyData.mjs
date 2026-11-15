// Real Chromium. The fixes from the benchmark on unfamiliar data:
//  - unit="currency" uses the data's own currency (a code in the column name,
//    else the viz's saved format), not USD;
//  - RankChart has no value-axis tick labels (every bar carries its value);
//  - a raw EChart with more than 8 line series shows the 8 largest and keeps
//    the rest greyed in a scrolling legend;
//  - DataTable on a right-to-left page runs right to left, even with English cells;
//  - cards side by side in a grid row are the same height.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';
const root=new URL('../../',import.meta.url);
const iframeJs=ts.transpileModule(fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const VIZ='5d3b2a19-8d7e-4f6a-9b0c-1d2e3f4a5b6c', EURVIZ='6e3b2a19-8d7e-4f6a-9b0c-1d2e3f4a5b6c';
const months=Array.from({length:12},(_,i)=>`2025-${String(i+1).padStart(2,'0')}`);
const rows=months.flatMap((m,i)=>['A','B'].map((d,j)=>({month:m,dept:d,amount_eur:1000*(i+1)+j,spend:500*(i+1)})));
const lines=Array.from({length:12},(_,k)=>({name:`CC-${100+k}`,type:'line',data:months.map((_,i)=>(k+1)*100+i)}));
const app=`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  return (
    <div className="p-4 space-y-4">
      <div id="lines"><EChart viz={viz} option={${JSON.stringify({xAxis:{type:'category',data:months},yAxis:{type:'value'},series:lines})}} height={260} /></div>
      <div id="table"><DataTable viz={viz} rows={[{team:'Tier 1',tickets:120},{team:'Tier 2',tickets:80}]} columns={[{field:'team',headerName:'Team'},{field:'tickets',headerName:'Tickets'}]} /></div>
      <div id="grid" className="grid grid-cols-2 gap-4">
        <SectionCard title="Short"><p>one line</p></SectionCard>
        <SectionCard title="Tall"><p>one</p><p>two</p><p>three</p><p>four</p><p>five</p></SectionCard>
      </div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true, executablePath:CHROME_PATH});
try{
 const page=await browser.newPage({viewport:{width:900,height:1200}});
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
  return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
 });
 await page.goto('http://artifact-review.test/');
 await page.addScriptTag({content:'window.exports={};'+iframeJs});
 await page.evaluate(({code,VIZ,EURVIZ,rows})=>{
  const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[
    {id:VIZ,title:'Spend',rows,columns:[{field:'month'},{field:'dept'},{field:'amount_eur'},{field:'spend'}]},
    {id:EURVIZ,title:'Spend (saved in EUR)',rows,columns:[{field:'month'},{field:'dept'},{field:'spend'}],view:{version:'v2',view:{type:'bar_chart',axisY:{format:'currency',currency:'EUR'}}}},
  ]}});
  const f=document.createElement('iframe');f.style.width='900px';f.style.height='1180px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
 },{code:app,VIZ,EURVIZ,rows});
 const frame=page.frames().find(f=>f.parentFrame());
 await frame.waitForFunction(()=>window.__bowCharts&&window.echarts&&document.querySelector('#lines [_echarts_instance_]')&&document.querySelector('#table table'),null,{timeout:30000});
 await frame.waitForTimeout(400);

 // Currency: the column name, then the viz's saved format, then USD; an explicit code always wins.
 const cur=await frame.evaluate(({VIZ,EURVIZ})=>{
  const B=window.__bowCharts, theme=window.bowTheme, v=vizById(VIZ), e=vizById(EURVIZ);
  const tick=(b)=>b.option.yAxis.axisLabel.formatter(2000);
  const bar=(b)=>b.option.series[0].label.formatter({value:2500});
  return {
   name:tick(B.buildTrend({viz:v,x:'month',y:'amount_eur',series:'dept',unit:'currency',additive:true},theme,'en')),
   saved:tick(B.buildTrend({viz:e,x:'month',y:'spend',series:'dept',unit:'currency',additive:true},theme,'en')),
   plain:tick(B.buildTrend({viz:v,x:'month',y:'spend',series:'dept',unit:'currency',additive:true},theme,'en')),
   code:tick(B.buildTrend({viz:v,x:'month',y:'spend',series:'dept',unit:'GBP',additive:true},theme,'en')),
   rankName:bar(B.buildRank({viz:v,label:'dept',value:'amount_eur',unit:'currency',additive:true},theme,'en',false)),
   rankAxis:B.buildRank({viz:v,label:'dept',value:'amount_eur',unit:'currency',additive:true},theme,'en',false).option.xAxis.axisLabel.show,
  };
 },{VIZ,EURVIZ});
 assert.match(cur.name,/€/,`a code in the column name sets the currency (${cur.name})`);
 assert.match(cur.saved,/€/,`the viz's saved currency is used (${cur.saved})`);
 assert.match(cur.plain,/\$/,`with neither, USD (${cur.plain})`);
 assert.match(cur.code,/£/,`an explicit code wins (${cur.code})`);
 assert.match(cur.rankName,/€/,`RankChart resolves the currency too (${cur.rankName})`);
 assert.equal(cur.rankAxis,false,'RankChart hides its value-axis tick labels: every bar carries its value');

 // Raw EChart, 12 line series: the 8 largest shown, the rest greyed in a scrolling legend.
 const leg=await frame.evaluate(()=>{
  const el=document.querySelector('#lines [_echarts_instance_]'); const o=window.echarts.getInstanceByDom(el).getOption(); const l=o.legend[0];
  const on=Object.entries(l.selected||{}).filter(([,v])=>v).map(([k])=>k).sort();
  return {type:l.type, on, off:Object.values(l.selected||{}).filter(v=>!v).length};
 });
 assert.equal(leg.type,'scroll');
 assert.equal(leg.off,4,'4 of 12 series start greyed');
 assert.deepEqual(leg.on,['CC-104','CC-105','CC-106','CC-107','CC-108','CC-109','CC-110','CC-111'],'the 8 largest by total stay on');

 // Equal card heights in a grid row.
 const h=await frame.evaluate(()=>[...document.querySelectorAll('#grid > section')].map(s=>Math.round(s.getBoundingClientRect().height)));
 assert.equal(h.length,2); assert.equal(h[0],h[1],`cards in a grid row are the same height (${h})`);

 // DataTable on a right-to-left page, English cells: right to left.
 const ltrDir=await frame.evaluate(()=>document.querySelector('[data-testid=bow-datatable]').getAttribute('dir'));
 assert.equal(ltrDir,'ltr','on a left-to-right page the English table stays left to right');
 const rtl=await page.evaluate(({code,VIZ,EURVIZ,rows})=>new Promise(res=>{
  const html=exports.buildArtifactIframeHtml({code:code.replace('<div className="p-4 space-y-4">','<div className="p-4 space-y-4" dir="rtl">').replace("ReactDOM.createRoot","document.documentElement.setAttribute('dir','rtl'); ReactDOM.createRoot"),data:{runtime:{version:11},visualizations:[{id:VIZ,title:'Spend',rows,columns:[{field:'month'},{field:'dept'},{field:'amount_eur'},{field:'spend'}]},{id:EURVIZ,title:'x',rows,columns:[]}]}});
  const f=document.createElement('iframe');f.id='rtl';f.style.width='900px';f.style.height='600px';f.srcdoc=html;f.onload=()=>res(true);document.body.appendChild(f);
 }),{code:app,VIZ,EURVIZ,rows});
 const rf=page.frames().filter(f=>f.parentFrame())[1];
 await rf.waitForFunction(()=>document.querySelector('[data-testid=bow-datatable]'),null,{timeout:30000});
 const rtlDir=await rf.evaluate(()=>document.querySelector('[data-testid=bow-datatable]').getAttribute('dir'));
 assert.equal(rtlDir,'rtl','on a right-to-left page the table runs right to left, even with English cells');
 console.log('PASS: data currency on components, RankChart axis, 8 of 12 series, equal card heights, RTL tables');
}finally{await browser.close()}
