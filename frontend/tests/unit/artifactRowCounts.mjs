// A cut query result reaches artifact code as cut: `vizById(id)` exposes the
// real size, the loaded rows and `truncated`, on both page builders, and a
// filter re-run updates them with its rows. Real Chromium for the page half.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';
const root=new URL('../../',import.meta.url);
const load=f=>{const m={exports:{}};new Function('exports','module',ts.transpileModule(fs.readFileSync(new URL(f,root),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)(m.exports,m);return m.exports};
const {artifactRowCounts:counts}=load('utils/artifactRowCounts.ts');
const rows=n=>Array.from({length:n},(_,i)=>({i}));

assert.deepEqual(counts({rows:rows(1000),info:{total_rows:2240}}),{row_count:2240,loaded_row_count:1000,truncated:true});
assert.deepEqual(counts({rows:rows(7),info:{total_rows:7}}),{row_count:7,loaded_row_count:7,truncated:false});
for(const d of [undefined,null,{},{rows:[]},{rows:rows(2)},{rows:rows(2),info:{total_rows:1}},{rows:rows(2),info:{total_rows:'9'}}]){
  const c=counts(d); assert.equal(c.truncated,false,JSON.stringify(d)); assert.equal(c.row_count,c.loaded_row_count);
}
console.log('PASS: real size, loaded rows and truncated; nonsense totals never claim a cut');

// Every place that hands rows to a page hands the counts with them.
for(const f of ['components/dashboard/ArtifactFrame.vue','components/dashboard/SharedDashboardView.vue']){
  const src=fs.readFileSync(new URL(f,root),'utf8');
  const built=[...src.matchAll(/rows: [^\n]*data\?\.rows \|\| \[\],\n\s*(\.\.\.artifactRowCounts\()?/g)];
  assert.ok(built.length>0,`${f}: found the page builder`);
  for(const m of built) assert.ok(m[1],`${f}: rows without counts: ${m[0].trim()}`);
  const reruns=[...src.matchAll(/viz\.rows = res\.data\?\.rows \|\| \[\];\n\s*(Object\.assign\(viz, artifactRowCounts\(res\.data\)\))?/g)];
  assert.equal(reruns.length,1,`${f}: one filter re-run`);
  assert.ok(reruns[0][1],`${f}: a filter re-run must update the counts with the rows`);
}
console.log('PASS: both page builders and both filter re-runs carry the counts');

const source=fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const VIZ='7c1d0a52-11aa-4b1e-9d3f-5a6b7c8d9e01';
const app=`<script type="text/babel">
function App(){const v=vizById("${VIZ}");return <div id="out">{v.rows.length}/{v.row_count}/{v.loaded_row_count}/{String(v.truncated)}</div>}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true,executablePath:CHROME_PATH});
try{
  const page=await browser.newPage();
  await page.route('**/*',async route=>{const u=new URL(route.request().url());
    if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
    return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'})});
  await page.goto('http://artifact-review.test/');
  await page.addScriptTag({content:'window.exports={};'+compiled});
  await page.evaluate(({code,VIZ,c,n})=>{
    const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[{id:VIZ,title:'Sales',rows:Array.from({length:n},(_,i)=>({i})),columns:[{field:'i'}],...c}]}});
    const f=document.createElement('iframe');f.srcdoc=html;document.body.appendChild(f);
  },{code:app,VIZ,c:counts({rows:rows(40),info:{total_rows:2240}}),n:40});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForSelector('#out');
  assert.equal(await frame.textContent('#out'),'40/2240/40/true');
  console.log('PASS: artifact code reads the counts through vizById');
}finally{await browser.close()}
