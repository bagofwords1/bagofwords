// Real Chromium, DataTable width:
//  - on a phone, a wide table in a grid or flex cell without min-width: 0
//    scrolls inside its own box (LTR and RTL). Before, the cell grew to the
//    table's width and the whole page scrolled sideways;
//  - a visibility class on the table (hidden md:block) still decides whether it shows;
//  - a parent that sizes to its content (inline-block, a flex item that does
//    not grow, a centred column, a chat bubble, a table cell) still gets the
//    table's full width instead of a collapsed strip.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';
const root=new URL('../../',import.meta.url);
const source=fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const WIDE='7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f', SMALL='8d2e3f4a-5b6c-4d7e-9f0a-1b2c3d4e5f60';
const wideCols=['Album','Artist','Genre','Tracks','Minutes','Price','Released','Label'];
const wideRows=[...Array(6)].map((_,i)=>Object.fromEntries(wideCols.map(c=>[c,`${c} value number ${i}`])));
const smallCols=['Rep','Region','Deals'];
const smallRows=[...Array(4)].map((_,i)=>({Rep:`Rep ${i}`,Region:'EMEA',Deals:i}));
const narrowApp=dir=>`<script type="text/babel">
function App() {
  const viz = vizById("${WIDE}");
  return (
    <div dir="${dir}" className="p-4">
      <div className="grid gap-4 lg:grid-cols-3"><div className="lg:col-span-2"><DataTable viz={viz} /></div></div>
      <div className="flex"><div className="flex-1"><DataTable viz={viz} /></div></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const fitApp=`<script type="text/babel">
function App() {
  const viz = vizById("${SMALL}");
  return (
    <div className="p-4 space-y-6">
      <div className="inline-block" data-case="inline-block"><DataTable viz={viz} /></div>
      <div className="flex gap-4"><div data-case="flex item"><DataTable viz={viz} /></div><div>chart</div></div>
      <div className="flex flex-col items-center"><div data-case="centred column"><DataTable viz={viz} /></div></div>
      <div className="flex flex-col"><div className="self-start max-w-[85%]" data-case="chat bubble"><p>Here you go:</p><DataTable viz={viz} /></div></div>
      <table><tbody><tr><td data-case="table cell"><DataTable viz={viz} /></td></tr></tbody></table>
      <div id="hidden-case"><DataTable viz={viz} className="hidden md:block" /></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
async function render(browser,width,code,expected){
  const page=await browser.newPage({viewport:{width,height:900}});
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
   return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
  });
  await page.goto('http://artifact-review.test/');
  await page.addScriptTag({content:'window.exports={};'+compiled});
  await page.evaluate(({code,width,WIDE,SMALL,wideRows,wideCols,smallRows,smallCols})=>{
   const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[
     {id:WIDE,title:'Albums',rows:wideRows,columns:wideCols.map(field=>({field}))},
     {id:SMALL,title:'Reps',rows:smallRows,columns:smallCols.map(field=>({field}))}]}});
   const f=document.createElement('iframe');f.style.width=width+'px';f.style.height='880px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
  },{code,width,WIDE,SMALL,wideRows,wideCols,smallRows,smallCols});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForFunction(n=>document.querySelectorAll('.bow-dt-scroll table').length>=n,expected,{timeout:30000});
  return {page,frame};
}
const browser=await chromium.launch({headless:true, executablePath:CHROME_PATH});
try{
 for(const dir of ['ltr','rtl']){
  const {page,frame}=await render(browser,390,narrowApp(dir),2);
  const r=await frame.evaluate(()=>({
   page:[document.documentElement.scrollWidth,document.documentElement.clientWidth],
   boxes:[...document.querySelectorAll('.bow-dt-scroll')].map(b=>[b.clientWidth,b.scrollWidth]),
  }));
  assert.ok(r.page[0]<=r.page[1]+1,`${dir}: the page does not scroll sideways (${r.page})`);
  for(const [client,scroll] of r.boxes){
   assert.ok(client<=r.page[1],`${dir}: the table box fits the screen (${client})`);
   assert.ok(client>=r.page[1]-40,`${dir}: the table box uses the screen width, not a strip (${client})`);
   assert.ok(scroll>client,`${dir}: the wide table scrolls inside its box (${scroll} > ${client})`);
  }
  await page.close();
 }
 const {page,frame}=await render(browser,960,fitApp,5);
 // A visibility class on the table still wins: hidden below md, shown from md (768px) up.
 const shownAt960=await frame.evaluate(()=>getComputedStyle(document.querySelector('#hidden-case [data-testid="bow-datatable"]')).display);
 assert.notEqual(shownAt960,'none','className="hidden md:block" shows the table at 960px');
 await page.setViewportSize({width:600,height:900});
 await page.evaluate(()=>{document.querySelector('iframe').style.width='600px'});
 await frame.waitForFunction(()=>innerWidth<700,null,{timeout:10000});
 const shownAt600=await frame.evaluate(()=>getComputedStyle(document.querySelector('#hidden-case [data-testid="bow-datatable"]')).display);
 assert.equal(shownAt600,'none','className="hidden md:block" hides the table below 768px');
 await page.evaluate(()=>{document.querySelector('iframe').style.width='960px'});
 await frame.waitForFunction(()=>innerWidth>=900,null,{timeout:10000});
 const fit=await frame.evaluate(()=>[...document.querySelectorAll('[data-case]')].map(c=>{const b=c.querySelector('.bow-dt-scroll');const t=b.querySelector('table');return [c.dataset.case,b.clientWidth,t.getBoundingClientRect().width]}));
 for(const [name,box,table] of fit){
  assert.ok(table>150,`${name}: the table has real columns (${table})`);
  assert.ok(box>=table-1,`${name}: the box shows the whole table, not a collapsed strip (box ${box}, table ${table})`);
 }
 await page.close();
 console.log('PASS: a wide DataTable scrolls inside its box on a phone; content-sized parents keep its full width');
}finally{await browser.close()}
