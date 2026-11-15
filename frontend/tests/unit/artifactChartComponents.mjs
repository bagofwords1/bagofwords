// Real Chromium: TrendChart / RankChart builders and rendering.
//  - time values read by pattern in UTC (timestamps with Z or an offset, weeks,
//    quarters, integer years), grain from the data;
//  - too many points → the latest window (several series: 24, one: 60), or an
//    opt-in roll-up for additive measures that drops partial edge buckets;
//  - duplicate keys and missing periods follow `additive`;
//  - units: percent (already ×100) vs ratio (0–1);
//  - rankings: every name shown, top N + Other only when additive, ties by name;
//  - errors are shown, never thrown; time labels stay horizontal, spaced by width;
//  - the last point is marked and a single line shows its latest value;
//  - one ⓘ for a chart in a card, on a plain card and on a bare chart.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';
const root=new URL('../../',import.meta.url);
const compile=f=>ts.transpileModule(fs.readFileSync(new URL(f,root),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const iframeJs=compile('utils/artifactIframe.ts');
const VIZ='1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d';
const months=[]; for(let y=2021;y<=2025;y++) for(let m=1;m<=12;m++) months.push(`${y}-${String(m).padStart(2,'0')}`);
const reps=['Rep A','Rep B','Rep C'];
const repRows=months.flatMap((m,i)=>reps.map((r,j)=>({month:m,rep:r,revenue:10+((i*7+j*3)%20)})));
const app=`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  return (
    <div className="p-4 space-y-4">
      <div id="card"><SectionCard title="In a card" viz={viz}><TrendChart viz={viz} x="month" y="revenue" series="rep" unit="currency" additive /></SectionCard></div>
      <div id="plain"><SectionCard title="Plain card" viz={viz}><p>no chart</p></SectionCard></div>
      <div id="bare"><RankChart viz={viz} label="rep" value="revenue" unit="currency" additive /></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true, executablePath:CHROME_PATH});
try{
 for(const scheme of ['light','dark']){
 const page=await browser.newPage({viewport:{width:390,height:900},colorScheme:scheme});
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
  return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
 });
 await page.goto('http://artifact-review.test/');
 await page.addScriptTag({content:'window.exports={};'+iframeJs});
 await page.evaluate(({code,VIZ,rows})=>{
  const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[{id:VIZ,title:'Revenue',rows,columns:[{field:'month'},{field:'rep'},{field:'revenue'}]}]}});
  const f=document.createElement('iframe');f.style.width='390px';f.style.height='880px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
 },{code:app,VIZ,rows:repRows});
 const frame=page.frames().find(f=>f.parentFrame());
 await frame.waitForFunction(()=>window.__bowCharts&&document.querySelectorAll('[data-bow-chart] canvas').length===2,null,{timeout:30000});
 await frame.waitForTimeout(400);

 if(scheme==='light'){
 const r=await frame.evaluate(({rows,months})=>{
  const B=window.__bowCharts, theme=window.bowTheme;
  const viz={id:'v',rows,columns:[{field:'month'},{field:'rep'},{field:'revenue'}]};
  const out={};
  const p=B.parseTime;
  out.parse=[p('2025-01'),p('2025-01-01T00:00:00.000'),p('2025-01-01T00:00:00Z'),p('2025-01-01T23:30:00+02:00'),p(2025),p('2025-Q2'),p('2025-W07'),p('Jan')].map(x=>x&&[new Date(x.t).toISOString().slice(0,10),x.hint]);
  const many=B.buildTrend({viz,x:'month',y:'revenue',series:'rep',unit:'currency',additive:true},theme,'en');
  out.many={points:many.option.xAxis.data.length,first:many.option.xAxis.data[0],caption:many.caption,series:many.option.series.length,smooth:many.option.series.some(s=>s.smooth)};
  out.axis=many.option.xAxis.axisLabel; out.marks=many.option.series.every(s=>s.markPoint&&s.markPoint.data[0].coord[0]===23);
  const one=B.buildTrend({viz:{...viz,rows:rows.filter(r=>r.rep==='Rep A')},x:'month',y:'revenue',unit:'currency'},theme,'en');
  out.one={points:one.option.xAxis.data.length,caption:one.caption,endLabel:one.option.series[0].endLabel&&one.option.series[0].endLabel.formatter({value:12.5})};
  const part=rows.filter(r=>r.rep==='Rep A'&&r.month>='2023-02'&&r.month<='2025-03');
  const roll=B.buildTrend({viz:{...viz,rows:part},x:'month',y:'revenue',unit:'currency',additive:true,overflow:'rollup',maxPoints:12},theme,'en');
  out.roll={labels:roll.option.xAxis.data,caption:roll.caption,first:roll.option.series[0].data[0]};
  const rollNo=B.buildTrend({viz:{...viz,rows:part},x:'month',y:'revenue',unit:'percent',overflow:'rollup',maxPoints:12},theme,'en');
  out.rollNo={points:rollNo.option.xAxis.data.length,caption:rollNo.caption};
  const dupRows=[{month:'2025-01',v:1},{month:'2025-01',v:2},{month:'2025-03',v:5}];
  out.dupNo=B.buildTrend({viz:{id:'d',rows:dupRows,columns:[{field:'month'},{field:'v'}]},x:'month',y:'v'},theme,'en').error||null;
  const dupYes=B.buildTrend({viz:{id:'d',rows:dupRows,columns:[{field:'month'},{field:'v'}]},x:'month',y:'v',additive:true},theme,'en');
  out.dupYes=dupYes.option.series[0].data.map(d=>d&&typeof d==='object'?d.value:d);
  const gapRows=[{month:'2025-01',v:1},{month:'2025-03',v:5}];
  out.gap=B.buildTrend({viz:{id:'g',rows:gapRows,columns:[{field:'month'},{field:'v'}]},x:'month',y:'v'},theme,'en').option.series[0].data.map(d=>d&&typeof d==='object'?d.value:d);
  const days=[...Array(90)].map((_,i)=>({day:new Date(Date.UTC(2025,0,1)+i*86400000).toISOString().replace('.000',''),v:i}));
  const daily=B.buildTrend({viz:{id:'dd',rows:days,columns:[{field:'day'},{field:'v'}]},x:'day',y:'v'},theme,'en');
  out.daily={points:daily.option.xAxis.data.length,caption:daily.caption};
  out.missing=B.buildTrend({viz,x:'month',y:'sales'},theme,'en').error||null;
  out.both=B.buildTrend({viz,x:'month',y:['revenue','rep'],series:'rep'},theme,'en').error||null;
  out.noViz=B.buildTrend({viz:null,x:'month',y:'revenue'},theme,'en').error||null;
  out.empty=!!B.buildTrend({viz:{...viz,rows:[]},x:'month',y:'revenue'},theme,'en').empty;
  out.derived=!B.buildTrend({viz,rows:[{month:'2025-01',total:3}],x:'month',y:'total'},theme,'en').error;
  const mk=(rows,props)=>B.buildTrend({viz:{id:'s',rows,columns:Object.keys(rows[0]).map(field=>({field}))},...props},theme,'en');
  const sparse=mk([{d:'2025-01-01',v:1},{d:'2025-01-29',v:2}],{x:'d',y:'v'});
  out.sparse={err:sparse.error||null,points:sparse.option&&sparse.option.xAxis.data.length,first:sparse.option&&sparse.option.series[0].data[0],last:sparse.option&&sparse.option.series[0].data.slice(-1)[0]};
  out.weekly=mk([...Array(10)].map((_,i)=>({d:new Date(Date.UTC(2025,0,5)+i*7*86400000).toISOString().slice(0,10),v:i})),{x:'d',y:'v'}).option.xAxis.data.length;
  out.monthEnd=mk(['2025-01-31','2025-02-28','2025-03-31','2025-04-30'].map((d,i)=>({d,v:i})),{x:'d',y:'v'}).option.xAxis.data;
  out.explicitDay=mk([{d:'2025-01-01',v:1},{d:'2025-02-01',v:2}],{x:'d',y:'v',grain:'day'}).option.xAxis.data.length;
  const clk=mk([{m:'2025-01',v:1},{m:'2025-02',v:2}],{x:'m',y:'v'});
  out.clickKey=clk.click({dataIndex:0,seriesName:'v',value:1}).x;
  const wide=mk([{d:'2000-01-01',v:1},{d:'2000-01-02',v:2},{d:'2025-01-01',v:3}],{x:'d',y:'v'});
  out.newest=wide.click({dataIndex:wide.option.xAxis.data.length-1,seriesName:'v',value:3}).x;
  const proto=B.buildRank({viz:{id:'p',rows:[{n:'constructor',v:5},{n:'toString',v:3},{n:'A',v:1}],columns:[{field:'n'},{field:'v'}]},label:'n',value:'v',additive:true},theme,'en',false);
  out.proto=proto.error||proto.option.yAxis.data;
  out.units=[B.number('percent',12.5,1,'en'),B.number('ratio',0.125,1,'en'),B.number('currency',20,0,'en'),B.number('EUR',1234.5,0,'en')];
  const cty=[...Array(21)].map((_,i)=>({country:'Country '+String.fromCharCode(65+i),rev:i<2?50:21-i}));
  const rank=B.buildRank({viz:{id:'c',rows:cty,columns:[{field:'country'},{field:'rev'}]},label:'country',value:'rev',unit:'currency',additive:true},theme,'en',false);
  out.rank={bars:rank.option.yAxis.data,interval:rank.option.yAxis.axisLabel.interval,hide:rank.option.yAxis.axisLabel.hideOverlap,height:rank.height,caption:rank.caption};
  const big=B.buildRank({viz:{id:'b',rows:[{n:'A',v:826.65},{n:'B',v:40}],columns:[{field:'n'},{field:'v'}]},label:'n',value:'v',unit:'currency'},theme,'en',false);
  out.barLabel=big.option.series[0].label.formatter({value:826.65});
  out.barRoom=[big.option.grid.right, B.buildRank({viz:{id:'b',rows:[{n:'A',v:826.65}],columns:[{field:'n'},{field:'v'}]},label:'n',value:'v'},theme,'he',true).option.grid.left];
  const counts=B.buildTrend({viz:{id:'k',rows:[{m:'2025-01',c:58},{m:'2025-02',c:59}],columns:[{field:'m'},{field:'c'}]},x:'m',y:'c'},theme,'en');
  out.countLabel=counts.option.series[0].endLabel.formatter({value:59});
  const rankNo=B.buildRank({viz:{id:'c',rows:cty,columns:[{field:'country'},{field:'rev'}]},label:'country',value:'rev',unit:'number'},theme,'en',false);
  out.rankNo={bars:rankNo.option.yAxis.data.length,caption:rankNo.caption};
  const rankRtl=B.buildRank({viz:{id:'c',rows:cty,columns:[{field:'country'},{field:'rev'}]},label:'country',value:'rev'},theme,'he',true);
  out.rankRtl={inverse:rankRtl.option.xAxis.inverse,pos:rankRtl.option.yAxis.position,caption:rankRtl.caption};
  out.he=B.buildTrend({viz,x:'month',y:'revenue',series:'rep',additive:true},theme,'he').caption;
  // Derived rows filtered to none: "No data", not a missing-field error against the viz's columns.
  const vz={id:'d',rows:[{month:'2025-01',revenue:1}],columns:[{field:'month'},{field:'revenue'}]};
  out.emptyTrend=B.buildTrend({viz:vz,rows:[],x:'month',y:'total'},theme,'en');
  out.emptyRank=B.buildRank({viz:vz,rows:[],label:'team',value:'total'},theme,'en',false);
  // A ranking's click returns the source key: numeric ids stay numbers.
  const ids=B.buildRank({viz:{id:'i',rows:[{id:101,v:5},{id:7,v:3}],columns:[{field:'id'},{field:'v'}]},label:'id',value:'v'},theme,'en',false);
  out.rankKey=ids.click({dataIndex:0}).label;
  return out;
 },{rows:repRows,months});
 assert.deepEqual(r.parse,[['2025-01-01','month'],['2025-01-01',null],['2025-01-01',null],['2025-01-01',null],['2025-01-01','year'],['2025-04-01','quarter'],['2025-02-10','week'],null],'time values read by pattern, in UTC');
 assert.deepEqual(r.many,{points:24,first:'Jan 2024',caption:'Latest 24 of 60 months',series:3,smooth:false},'several series over 60 months: the latest 24, same grain');
 assert.deepEqual(r.one,{points:60,caption:'',endLabel:'$13'},'one series keeps 60 points and shows its latest value (whole-dollar data, no cents)');
 assert.deepEqual({interval:r.axis.interval,rotate:r.axis.rotate},{interval:'auto',rotate:0},'time labels horizontal, spaced by width');
 assert.equal(r.marks,true,'the last point of each line is marked');
 assert.equal(r.barLabel,'$827','bar values in whole units from 100 up');
 assert.ok(r.barRoom[0]>=48&&r.barRoom[1]>=48,`room for bar-end values on the side bars grow to (${r.barRoom})`);
 assert.equal(r.countLabel,'59','whole-number data prints without decimals');
 assert.deepEqual(r.roll.labels,['Q2 2023','Q3 2023','Q4 2023','Q1 2024','Q2 2024','Q3 2024','Q4 2024','Q1 2025'],'roll-up drops the partial first quarter');
 assert.match(r.roll.caption,/26 months grouped into 8 quarters; incomplete periods at the ends left out/);
 assert.deepEqual(r.rollNo,{points:12,caption:'Latest 12 of 26 months'},'roll-up without additive falls back to the latest window');
 assert.match(r.dupNo,/one row per month/,'duplicate keys without additive are a visible error');
 assert.deepEqual(r.dupYes,[3,0,5],'additive sums duplicates and fills missing periods with 0');
 assert.deepEqual(r.gap,[1,null,5],'non-additive missing periods are gaps');
 assert.deepEqual(r.daily,{points:60,caption:'Latest 60 of 90 days'},'daily timestamps: grain day, latest window');
 assert.match(r.missing,/no field named sales.*Available fields: month, rep, revenue/);
 assert.match(r.both,/either series or several y/);
 assert.match(r.noViz,/needs viz/);
 assert.equal(r.empty,true,'empty rows are a no-data state, not an error');
 assert.equal(r.derived,true,'derived rows with their own fields are valid');
 assert.equal(r.sparse.err,null,'sparse daily dates are not merged into one month');
 assert.ok(r.sparse.points>=2,'Jan 1 and Jan 29 stay separate points'); assert.equal(r.sparse.first,1); assert.equal(r.sparse.last,2);
 assert.equal(r.explicitDay,32,'an explicit finer grain is honoured');
 assert.equal(r.weekly,10,'weekly data on Sundays stays weekly');
 assert.deepEqual(r.monthEnd,['Jan 2025','Feb 2025','Mar 2025','Apr 2025'],'month-end dates are months');
 assert.equal(r.clickKey,'2025-01','onClick returns the source key');
 assert.equal(r.emptyTrend.empty,true,'TrendChart on empty derived rows: No data'); assert.equal(r.emptyTrend.error,undefined);
 assert.equal(r.emptyRank.empty,true,'RankChart on empty derived rows: No data'); assert.equal(r.emptyRank.error,undefined);
 assert.equal(r.rankKey,101,'RankChart onClick returns the raw key (101, not "101")');
 assert.equal(r.newest,'2025-01-01','a long range keeps the newest period');
 assert.deepEqual(r.proto,['constructor','toString','A'],'labels named like object properties are kept');
 assert.deepEqual(r.units,['12.5%','12.5%','$20','€1,235'],'percent vs ratio, decimals from the caller');
 assert.equal(r.rank.bars.length,11,'top 10 + Other');
 assert.equal(r.rank.bars[10],'Other (11)');
 assert.deepEqual(r.rank.bars.slice(0,2),['Country A','Country B'],'ties broken by name');
 assert.equal(r.rank.interval,0); assert.equal(r.rank.hide,false,'every name shown');
 assert.ok(r.rank.height>=24+11*30,'height grows with the bar count');
 assert.deepEqual(r.rankNo,{bars:10,caption:'Top 10 of 21'},'not additive: no Other, a caption');
 assert.deepEqual(r.rankRtl,{inverse:true,pos:'right',caption:'מוצגים 10 מתוך 21'},'RTL ranking is mirrored, caption in Hebrew');
 assert.match(r.he,/^התקופה האחרונה: 24 מתוך 60 חודשים$/);
 }

 const dom=await frame.evaluate(()=>({
  card:document.querySelectorAll('#card button[aria-label="Details"]').length,
  plain:document.querySelectorAll('#plain button[aria-label="Details"]').length,
  bare:document.querySelectorAll('#bare button[aria-label="Details"]').length,
  overlay:document.querySelectorAll('[data-bow-ibtn]').length,
  caption:document.querySelector('#card [data-bow-chart-caption]')?.textContent,
  sideways:document.documentElement.scrollWidth>document.documentElement.clientWidth+1,
 }));
 assert.equal(dom.card,1,`${scheme}: one ⓘ for a chart inside a card with the same viz`);
 assert.equal(dom.plain,1,`${scheme}: one ⓘ on a plain card`);
 assert.equal(dom.bare,1,`${scheme}: a bare chart has its own ⓘ`);
 assert.equal(dom.overlay,0,`${scheme}: the host marker adds no overlay ⓘ`);
 assert.equal(dom.caption,'Latest 24 of 60 months',`${scheme}: the caption is shown under the chart`);
 assert.equal(dom.sideways,false,`${scheme}: nothing scrolls sideways at 390px`);
 await page.close();
 }
 console.log('PASS: TrendChart/RankChart builders, horizontal time labels, end values, one ⓘ, light and dark at 390px');
}finally{await browser.close()}
