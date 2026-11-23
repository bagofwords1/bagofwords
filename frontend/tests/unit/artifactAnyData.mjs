// Real Chromium. The fixes from the benchmark on unfamiliar data:
//  - unit="currency" uses the data's own currency (a code in the column name,
//    else the viz's saved format, else the one code the viz's or the page's
//    columns name), not USD;
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
const VIZ='5d3b2a19-8d7e-4f6a-9b0c-1d2e3f4a5b6c', EURVIZ='6e3b2a19-8d7e-4f6a-9b0c-1d2e3f4a5b6c', PLAIN='7f3b2a19-8d7e-4f6a-9b0c-1d2e3f4a5b6c', GBPVIZ='8a3b2a19-8d7e-4f6a-9b0c-1d2e3f4a5b6c';
const months=Array.from({length:12},(_,i)=>`2025-${String(i+1).padStart(2,'0')}`);
const rows=months.flatMap((m,i)=>['A','B'].map((d,j)=>({month:m,dept:d,amount_eur:1000*(i+1)+j,spend:500*(i+1)})));
const lines=Array.from({length:12},(_,k)=>({name:`CC-${100+k}`,type:'line',data:months.map((_,i)=>(k+1)*100+i)}));
const dsRows=months.map((m,i)=>Object.fromEntries([['month',m],...lines.map((l,k)=>[l.name,(k+1)*100+i])]));
const app=`<script type="text/babel">
const LINES=${JSON.stringify(lines)};
function App() {
  const viz = vizById("${VIZ}");
  const [n, setN] = React.useState(0);
  // Inline option, rebuilt on every render: a formatter (same content) and, from the 2nd click, new data (same shape).
  const live = { xAxis: { type: 'category', data: ${JSON.stringify(months)} }, yAxis: { type: 'value' }, tooltip: { formatter: (p) => String(p.value) },
    dataZoom: [{ type: 'inside' }],
    // From the 2nd click, new data that also swaps the two largest series' totals;
    // from the 3rd, CC-103 grows into the top eight and pushes CC-104 out.
    series: LINES.map((l, i) => ({ ...l, data: (n >= 2 && i >= 10 ? LINES[21 - i].data : l.data).map((v) => v + (n >= 2 ? 1 : 0) + (n >= 3 && i === 3 ? 10000 : 0)) })) };
  return (
    <div className="p-4 space-y-4">
      <button id="rerender" onClick={() => setN(n + 1)}>rerender</button>
      <div id="lines"><EChart viz={viz} option={${JSON.stringify({xAxis:{type:'category',data:months},yAxis:{type:'value'},series:lines})}} height={260} /></div>
      <div id="live"><EChart option={live} height={200} /></div>
      <div id="dataset"><EChart option={${JSON.stringify({dataset:{source:dsRows},xAxis:{type:'category'},yAxis:{type:'value'},series:lines.map(l=>({name:l.name,type:'line',encode:{x:'month',y:l.name}}))})}} height={200} /></div>
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
 await page.evaluate(({code,VIZ,EURVIZ,PLAIN,GBPVIZ,rows})=>{
  const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[
    {id:VIZ,title:'Spend',rows,columns:[{field:'month'},{field:'dept'},{field:'amount_eur'},{field:'spend'}]},
    {id:EURVIZ,title:'Spend (saved in EUR)',rows,columns:[{field:'month'},{field:'dept'},{field:'spend'}],view:{version:'v2',view:{type:'bar_chart',axisY:{format:'currency',currency:'EUR'}}}},
    {id:PLAIN,title:'Spend',rows,columns:[{field:'month'},{field:'dept'},{field:'spend'}]},
    {id:GBPVIZ,title:'UK costs',rows:[],columns:[{field:'cost_gbp'}]},
  ]}});
  const f=document.createElement('iframe');f.style.width='900px';f.style.height='1180px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
 },{code:app,VIZ,EURVIZ,PLAIN,GBPVIZ,rows});
 const frame=page.frames().find(f=>f.parentFrame());
 await frame.waitForFunction(()=>window.__bowCharts&&window.echarts&&document.querySelector('#lines [_echarts_instance_]')&&document.querySelector('#live [_echarts_instance_]')&&document.querySelector('#dataset [_echarts_instance_]')&&document.querySelector('#table table'),null,{timeout:30000});
 await frame.waitForTimeout(400);

 // Currency: the column name, the viz's saved format, the viz's columns, the page's columns
 // (one code only), then USD; an explicit code always wins.
 const cur=await frame.evaluate(({VIZ,EURVIZ,PLAIN})=>{
  const B=window.__bowCharts, theme=window.bowTheme, v=vizById(VIZ), e=vizById(EURVIZ), pl=vizById(PLAIN);
  const tick=(b)=>b.option.yAxis.axisLabel.formatter(2000);
  const bar=(b)=>b.option.series[0].label.formatter({value:2500});
  return {
   name:tick(B.buildTrend({viz:v,x:'month',y:'amount_eur',series:'dept',unit:'currency',additive:true},theme,'en')),
   saved:tick(B.buildTrend({viz:e,x:'month',y:'spend',series:'dept',unit:'currency',additive:true},theme,'en')),
   vizCols:tick(B.buildTrend({viz:v,x:'month',y:'spend',series:'dept',unit:'currency',additive:true},theme,'en')),
   plain:tick(B.buildTrend({viz:pl,x:'month',y:'spend',series:'dept',unit:'currency',additive:true},theme,'en')),
   code:tick(B.buildTrend({viz:v,x:'month',y:'spend',series:'dept',unit:'GBP',additive:true},theme,'en')),
   rankName:bar(B.buildRank({viz:v,label:'dept',value:'amount_eur',unit:'currency',additive:true},theme,'en',false)),
   rankAxis:B.buildRank({viz:v,label:'dept',value:'amount_eur',unit:'currency',additive:true},theme,'en',false).option.xAxis.axisLabel.show,
  };
 },{VIZ,EURVIZ,PLAIN});
 assert.match(cur.name,/€/,`a code in the column name sets the currency (${cur.name})`);
 assert.match(cur.saved,/€/,`the viz's saved currency is used (${cur.saved})`);
 assert.match(cur.vizCols,/€/,`a renamed column on a viz whose columns name EUR (${cur.vizCols})`);
 assert.match(cur.plain,/\$/,`no code on the viz and two codes on the page (EUR, GBP): USD (${cur.plain})`);
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

 // A reader's legend choice survives the parent re-rendering the same option, and new data merged in.
 const sel=()=>frame.evaluate(()=>window.echarts.getInstanceByDom(document.querySelector('#live [_echarts_instance_]')).getOption().legend[0].selected);
 assert.equal((await sel())['CC-100'],false,'CC-100 (smallest) starts greyed');
 await frame.evaluate(()=>window.echarts.getInstanceByDom(document.querySelector('#live [_echarts_instance_]')).dispatchAction({type:'legendToggleSelect',name:'CC-100'}));
 assert.equal((await sel())['CC-100'],true);
 await frame.click('#rerender'); await frame.waitForTimeout(300);
 assert.equal((await sel())['CC-100'],true,'same content re-render keeps the reader\'s choice');
 await frame.click('#rerender'); await frame.waitForTimeout(300);
 assert.equal((await sel())['CC-100'],true,'new data that swaps two totals keeps the reader\'s choice');
 // New data that moves a series across the eighth-place cutoff is still a data
 // refresh: the reader's legend click and zoom survive; series the reader never
 // touched follow the new top eight.
 const zoom=()=>frame.evaluate(()=>window.echarts.getInstanceByDom(document.querySelector('#live [_echarts_instance_]')).getOption().dataZoom[0].start);
 await frame.evaluate(()=>window.echarts.getInstanceByDom(document.querySelector('#live [_echarts_instance_]')).dispatchAction({type:'dataZoom',start:50,end:100}));
 assert.equal(await zoom(),50);
 await frame.click('#rerender'); await frame.waitForTimeout(300);
 const crossed=await sel();
 assert.equal(crossed['CC-100'],true,'a refresh that changes the top eight keeps the reader\'s choice');
 assert.equal(await zoom(),50,'a refresh that changes the top eight keeps the reader\'s zoom');
 assert.equal(crossed['CC-103'],true,'an untouched series that enters the top eight turns on');
 assert.equal(crossed['CC-104'],false,'an untouched series that leaves the top eight turns grey');

 // Series drawn from a dataset have no data of their own: the chart is left uncapped.
 const dsOff=await frame.evaluate(()=>{const l=window.echarts.getInstanceByDom(document.querySelector('#dataset [_echarts_instance_]')).getOption().legend; const s=l&&l[0]&&l[0].selected; return s?Object.values(s).filter(v=>!v).length:0;});
 assert.equal(dsOff,0,'a dataset-backed chart keeps every series on');

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
 // A viz whose columns name no currency, renamed column, on a page whose data names one (EUR): EUR.
 const pageCur=await rf.evaluate(({rows,EURVIZ})=>window.__bowCharts.buildTrend({viz:vizById(EURVIZ),rows:rows.map(r=>({month:r.month,dept:r.dept,amount:r.spend})),x:'month',y:'amount',series:'dept',unit:'currency',additive:true},window.bowTheme,'en').option.yAxis.axisLabel.formatter(2000),{rows,EURVIZ});
 assert.match(pageCur,/€/,`the page's single currency (${pageCur})`);
 assert.equal(rtlDir,'rtl','on a right-to-left page the table runs right to left, even with English cells');
 console.log('PASS: data currency on components (name, saved, viz columns, page), RankChart axis, 8 of 12 series (reader choice kept, dataset charts uncapped), equal card heights, RTL tables');
}finally{await browser.close()}
