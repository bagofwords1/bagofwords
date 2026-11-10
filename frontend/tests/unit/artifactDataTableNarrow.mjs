// Real Chromium: a wide DataTable on a phone scrolls inside its own box, even
// when the page puts it in a grid or flex cell without min-width: 0. Before,
// the cell grew to the table's width and the whole page scrolled sideways.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';
const root=new URL('../../',import.meta.url);
const source=fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const VIZ='7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f';
const cols=['Album','Artist','Genre','Tracks','Minutes','Price','Released','Label'];
const rows=[...Array(6)].map((_,i)=>Object.fromEntries(cols.map(c=>[c,`${c} value number ${i}`])));
const app=dir=>`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  return (
    <div dir="${dir}" className="p-4">
      <div className="grid gap-4 lg:grid-cols-3"><div className="lg:col-span-2" id="grid"><DataTable viz={viz} /></div></div>
      <div className="flex"><div className="flex-1" id="flex"><DataTable viz={viz} /></div></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true, executablePath:CHROME_PATH});
try{
 for(const dir of ['ltr','rtl']){
  const page=await browser.newPage({viewport:{width:390,height:800}});
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
   return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
  });
  await page.goto('http://artifact-review.test/');
  await page.addScriptTag({content:'window.exports={};'+compiled});
  await page.evaluate(({code,VIZ,rows,cols})=>{
   const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[{id:VIZ,title:'Albums',rows,columns:cols.map(field=>({field}))}]}});
   const f=document.createElement('iframe');f.style.width='390px';f.style.height='780px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
  },{code:app(dir),VIZ,rows,cols});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForFunction(()=>document.querySelectorAll('.bow-dt-scroll table').length===2,null,{timeout:30000});
  const r=await frame.evaluate(()=>({
   page:[document.documentElement.scrollWidth,document.documentElement.clientWidth],
   boxes:[...document.querySelectorAll('.bow-dt-scroll')].map(b=>[b.clientWidth,b.scrollWidth]),
  }));
  assert.ok(r.page[0]<=r.page[1]+1,`${dir}: the page does not scroll sideways (${r.page})`);
  for(const [client,scroll] of r.boxes){
   assert.ok(client<=r.page[1],`${dir}: the table box fits the screen (${client})`);
   assert.ok(scroll>client,`${dir}: the table scrolls inside its box (${scroll} > ${client})`);
  }
  await page.close();
 }
 console.log('PASS: a wide DataTable scrolls inside its box in grid and flex cells, LTR and RTL');
}finally{await browser.close()}
