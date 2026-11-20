// Real Chromium, DataTable column widths (dashboards):
//  - each column is as wide as its content needs (80–300px); a long header
//    over short values gets at most 140px and is cut with "…";
//  - long text stays on one line, cut with "…", full value in the tooltip —
//    every row keeps one height (before, a long text column took the whole
//    width and every row wrapped to two lines);
//  - spare width fills the card, so a narrow result still spans it;
//  - dragging a header's edge resizes only that column (LTR and RTL), does
//    not sort, and reports the layout to the host page, which hands it back
//    with the data (the frame has no storage of its own);
//  - dragging a header moves its column (LTR and RTL) like the results grid:
//    a label follows the pointer and the columns shift live; a plain click
//    still sorts, a drag does not; the order comes back saved;
//  - double-click on the edge puts the table back as built (widths, order);
//  - autoWidth={false} keeps the browser's own layout.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';
const root=new URL('../../',import.meta.url);
const source=fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const VIZ='5b0c1d2e-3f4a-4b5c-8d6e-7f8a9b0c1d2e';
const cols=['id','Customer feedback','Track name','Duration in milliseconds','Unit price'];
const rows=[...Array(12)].map((_,i)=>({
  id:i+1,
  'Customer feedback':`Customer ${i+1} wrote a very long comment explaining that the delivery arrived late, the packaging was damaged, and support answered quickly`,
  'Track name':`Track ${i+1}`,
  'Duration in milliseconds':180000+i*731,
  'Unit price':i%2?0.99:1.99,
}));
const app=(dir,extra='')=>`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  return <div dir="${dir}" className="p-4"><DataTable viz={viz} ${extra} /></div>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
async function render(browser,width,code,tableLayouts){
  const page=await browser.newPage({viewport:{width,height:900}});
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
   return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
  });
  await page.goto('http://artifact-review.test/');
  await page.addScriptTag({content:'window.exports={};'+compiled});
  await page.evaluate(({code,width,VIZ,rows,cols,tableLayouts})=>{
   window.__msgs=[];
   addEventListener('message',e=>{ if(e.data&&e.data.type==='ARTIFACT_TABLE_LAYOUT') window.__msgs.push(e.data); });
   const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},table_layouts:tableLayouts||{},visualizations:[
     {id:VIZ,title:'Tracks',rows,columns:cols.map(field=>({field}))}]}});
   const f=document.createElement('iframe');f.style.width=width+'px';f.style.height='880px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
  },{code,width,VIZ,rows,cols,tableLayouts});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForFunction(()=>document.querySelector('.bow-dt-scroll table'),null,{timeout:30000});
  await frame.waitForTimeout(400);
  return {page,frame};
}
const layout=(frame)=>frame.evaluate(()=>{
  const t=document.querySelector('.bow-dt-scroll table');
  const ths=[...t.tHead.rows[0].cells];
  const trs=[...t.tBodies[0].rows].filter(r=>r.offsetParent);
  const fb=trs[0].cells[1];
  return {
    fixed:getComputedStyle(t).tableLayout==='fixed',
    widths:ths.map(th=>Math.round(th.getBoundingClientRect().width)),
    rowHeights:[...new Set(trs.map(r=>Math.round(r.getBoundingClientRect().height)))],
    cellLine:Math.round(parseFloat(getComputedStyle(fb).lineHeight)||20),
    ellipsis:getComputedStyle(fb).textOverflow,
    title:fb.getAttribute('title')||'',
    box:document.querySelector('.bow-dt-scroll').clientWidth,
    table:Math.round(t.getBoundingClientRect().width),
    page:[document.documentElement.scrollWidth,document.documentElement.clientWidth],
    sort:ths.map(th=>th.getAttribute('aria-sort')),
    hasColgroup:!!t.querySelector('colgroup'),
    order:ths.map(th=>th.getAttribute('data-bow-dt-field')||th.textContent.trim()),
  };
});
const drag=async(page,frame,col,dx)=>{
  const box=await frame.locator('[data-testid="bow-dt-resize"]').nth(col).boundingBox();
  const x=box.x+box.width/2, y=box.y+box.height/2;
  await page.mouse.move(x,y); await page.mouse.down();
  for(let i=1;i<=8;i++) await page.mouse.move(x+dx*i/8,y);
  await page.mouse.up(); await frame.waitForTimeout(200);
};
const browser=await chromium.launch({headless:true, executablePath:CHROME_PATH});
try{
 // ---- Narrow card: content widths, caps, one line per row ----
 {
  const {page,frame}=await render(browser,700,app('ltr'));
  const r=await layout(frame);
  assert.ok(r.fixed,'sized table (fixed layout)');
  const [id,feedback,track,duration,price]=r.widths;
  assert.ok(id>=80&&id<=100,`id is narrow (${id})`);
  assert.equal(feedback,300,`long text stops at 300px (${feedback})`);
  assert.ok(duration<=141,`a long header over short numbers gets at most 140px (${duration})`);
  assert.ok(track>=80&&track<200,`short text stays short (${track})`);
  assert.equal(r.rowHeights.length,1,`every row has one height (${r.rowHeights})`);
  assert.ok(r.rowHeights[0]<r.cellLine*2+8,`rows are one line (${r.rowHeights[0]}px)`);
  assert.equal(r.ellipsis,'ellipsis','long text is cut with …');
  // Tooltips only where text is cut: hover a cut cell, then a whole one.
  const firstRow=frame.locator('tbody tr:not(.hidden)').first();
  await firstRow.locator('td').nth(1).hover();
  assert.match(await firstRow.locator('td').nth(1).getAttribute('title')||'',/Customer 1 wrote/,'a cut cell shows its full value on hover');
  await firstRow.locator('td').nth(2).hover();
  assert.equal(await firstRow.locator('td').nth(2).getAttribute('title'),null,'a cell shown whole has no tooltip');
  const longHead=frame.locator('th[data-bow-dt-field="Duration in milliseconds"]');
  await longHead.hover();
  assert.equal(await longHead.getAttribute('title'),'Duration in milliseconds','a cut header shows its full name on hover');
  const shortHead=frame.locator('th[data-bow-dt-field="id"]');
  await shortHead.hover();
  assert.equal(await shortHead.getAttribute('title'),null,'a header shown whole has no tooltip');
  // Print / PDF: the sheet lays the table out again; cells and headers wrap.
  await page.emulateMedia({media:'print'});
  const pr=await frame.evaluate(()=>{const t=document.querySelector('.bow-dt-table');const th=t.querySelector('th [data-bow-dt-label]');const td=t.querySelector('tbody td:nth-child(2)');return {layout:getComputedStyle(t).tableLayout,th:getComputedStyle(th).whiteSpace,td:getComputedStyle(td).whiteSpace,handles:[...document.querySelectorAll('[data-testid="bow-dt-resize"]')].every(h=>getComputedStyle(h).display==='none')};});
  assert.deepEqual(pr,{layout:'auto',th:'normal',td:'normal',handles:true},`print: auto layout, wrapping headers and cells, no handles (${JSON.stringify(pr)})`);
  await page.emulateMedia({media:'screen'});
  assert.ok(r.page[0]<=r.page[1]+1,`the page does not scroll sideways (${r.page})`);
  assert.ok(r.table>r.box,`a table wider than its card scrolls inside it (${r.table} > ${r.box})`);
  await page.close();
 }
 // ---- Wide card: spare width fills it, cut-off text first ----
 {
  const {page,frame}=await render(browser,1600,app('ltr'));
  const r=await layout(frame);
  assert.ok(Math.abs(r.table-r.box)<=2,`the table fills the card (${r.table} vs ${r.box})`);
  assert.ok(r.widths[1]>300,`spare room goes to the cut-off text column first (${r.widths[1]})`);
  // Resize: only that column changes, nothing sorts, the host hears about it.
  const before=r.widths;
  await drag(page,frame,2,90);
  const a=await layout(frame);
  assert.ok(Math.abs(a.widths[2]-(before[2]+90))<=3,`the dragged column grows by the drag (${before[2]} → ${a.widths[2]})`);
  for(const i of [0,1,3,4]) assert.ok(Math.abs(a.widths[i]-before[i])<=1,`column ${i} keeps its width (${before[i]} → ${a.widths[i]})`);
  assert.ok(a.sort.every(s=>!s),'a drag on the edge does not sort');
  const msgs=await page.evaluate(()=>window.__msgs);
  assert.ok(msgs.length>=1,'the new widths are sent to the host page');
  const last=msgs[msgs.length-1];
  assert.equal(last.key,`viz:${VIZ}`,'keyed by the visualization');
  assert.ok(Math.abs(last.widths['Track name']-a.widths[2])<=1,'with the dragged width');
  // A grab a few pixels past the line (the next column's start edge)
  // resizes the column before it, instead of sorting the next one.
  const mid=await layout(frame);
  const sb=await frame.locator('[data-testid="bow-dt-resize-start"]').nth(1).boundingBox();
  await page.mouse.move(sb.x+sb.width/2,sb.y+sb.height/2); await page.mouse.down();
  for(let i=1;i<=8;i++) await page.mouse.move(sb.x+sb.width/2+5*i,sb.y+sb.height/2);
  await page.mouse.up(); await frame.waitForTimeout(200);
  const s2=await layout(frame);
  assert.ok(Math.abs(s2.widths[1]-(mid.widths[1]+40))<=3,`the start edge of column 2 resizes column 1 (${mid.widths[1]} → ${s2.widths[1]})`);
  assert.ok(s2.sort.every(s=>!s),'and does not sort column 2');
  // Released where the frame never hears it: the next move (no button down) ends the drag.
  const eb=await frame.locator('[data-testid="bow-dt-resize"]').nth(0).boundingBox();
  await frame.locator('[data-testid="bow-dt-resize"]').nth(0).dispatchEvent('pointerdown',{button:0,buttons:1,clientX:eb.x+4,clientY:eb.y+4,pointerId:1,isPrimary:true});
  await frame.evaluate(()=>window.dispatchEvent(new PointerEvent('pointermove',{buttons:0,clientX:900,clientY:10})));
  assert.equal(await frame.evaluate(()=>document.documentElement.classList.contains('bow-dt-resizing')),false,'a move with no button down ends a drag whose release was lost');
  // Double-click: back to automatic, and the host forgets them.
  await frame.locator('[data-testid="bow-dt-resize"]').nth(2).dblclick();
  await frame.waitForTimeout(200);
  const reset=await layout(frame);
  assert.ok(Math.abs(reset.widths[2]-before[2])<=2,`double-click restores the automatic width (${reset.widths[2]} vs ${before[2]})`);
  const cleared=(await page.evaluate(()=>window.__msgs)).pop();
  assert.deepEqual([cleared.widths,cleared.order],[{},[]],'and tells the host to forget the layout');
  // ---- Move a column: drag "Unit price" before "Track name" ----
  const moveHeader=async(from,toCol,side)=>{
    const a=await frame.locator(`th[data-bow-dt-field="${from}"]`).boundingBox();
    const b=await frame.locator(`th[data-bow-dt-field="${toCol}"]`).boundingBox();
    const sx=a.x+a.width/2, y=a.y+a.height/2;
    const tx=side==='start'?b.x+8:b.x+b.width-8;
    await page.mouse.move(sx,y); await page.mouse.down();
    for(let i=1;i<=12;i++) await page.mouse.move(sx+(tx-sx)*i/12,y);
    return async()=>{ await page.mouse.up(); await frame.waitForTimeout(200); };
  };
  const release=await moveHeader('Unit price','Track name','start');
  // Like the results grid: a label follows the pointer and the columns have
  // already shifted before the release.
  const during=await frame.evaluate(()=>({ghost:(document.querySelector('.bow-dt-ghost')||{}).textContent||'',dragged:[...document.querySelectorAll('.bow-dt-dragged')].map(t=>t.getAttribute('data-bow-dt-field'))}));
  assert.equal(during.ghost,'Unit price','a label with the column name follows the pointer');
  assert.deepEqual(during.dragged,['Unit price'],'the moving column is marked where it sits');
  const live=(await layout(frame)).order;
  assert.deepEqual(live.slice(2,4),['Unit price','Track name'],`columns shift while dragging, before the release (${live})`);
  await release();
  assert.equal(await frame.locator('.bow-dt-ghost').count(),0,'the label is gone after the release');
  const moved=await layout(frame);
  assert.deepEqual(moved.order,['id','Customer feedback','Unit price','Track name','Duration in milliseconds'],`the column moves (${moved.order})`);
  assert.ok(moved.sort.every(s=>!s),'a move does not sort');
  const cells=await frame.$$eval('tbody tr:not(.hidden)',trs=>[...trs[0].cells].map(td=>td.textContent));
  assert.equal(cells[2],'1.99','the cells move with their header');
  const om=(await page.evaluate(()=>window.__msgs)).pop();
  assert.deepEqual(om.order,moved.order,'the new order is sent to the host');
  // A plain click (no travel) still sorts.
  await frame.locator('th[data-bow-dt-field="Track name"]').click();
  const sorted=await layout(frame);
  assert.ok(sorted.sort.some(s=>s==='ascending'),'a click without a drag sorts');
  await page.close();
 }
 // ---- RTL: dragging toward the left widens ----
 {
  const {page,frame}=await render(browser,1200,app('rtl','dir="rtl"'));
  const r=await layout(frame);
  await drag(page,frame,2,-70);
  const a=await layout(frame);
  assert.ok(Math.abs(a.widths[2]-(r.widths[2]+70))<=3,`RTL: dragging the edge left widens (${r.widths[2]} → ${a.widths[2]})`);
  // RTL move: drag "id" (rightmost) past "Customer feedback" (to its left).
  const ia=await frame.locator('th[data-bow-dt-field="id"]').boundingBox();
  const fb=await frame.locator('th[data-bow-dt-field="Customer feedback"]').boundingBox();
  const sx=ia.x+ia.width/2, y=ia.y+ia.height/2, tx=fb.x+10;
  await page.mouse.move(sx,y); await page.mouse.down();
  for(let i=1;i<=12;i++) await page.mouse.move(sx+(tx-sx)*i/12,y);
  await page.mouse.up(); await frame.waitForTimeout(200);
  const rm=await layout(frame);
  assert.deepEqual(rm.order.slice(0,2),['Customer feedback','id'],`RTL: the column moves past the one it was dropped on (${rm.order})`);
  await page.close();
 }
 // ---- Saved widths come back from the host ----
 {
  const {page,frame}=await render(browser,1200,app('ltr'),{[`viz:${VIZ}`]:{widths:{'Track name':333},order:['Track name','id']}});
  const r=await layout(frame);
  assert.deepEqual(r.order,['Track name','id','Customer feedback','Duration in milliseconds','Unit price'],`a saved order is applied; columns it doesn't know follow (${r.order})`);
  assert.ok(Math.abs(r.widths[0]-333)<=1,`a saved width is applied (${r.widths[0]})`);
  await page.close();
 }
 // ---- renderCell elements are sized by what they lay out to ----
 {
  const code=`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  const badge = (v, row, col) => col.field === 'Track name'
    ? <span className="inline-flex items-center px-4 py-0.5 rounded-full border text-xs">In progress — {v}</span> : null;
  return <div className="p-4"><DataTable viz={viz} renderCell={badge} /></div>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
  const {page,frame}=await render(browser,1600,code);
  const fit=await frame.evaluate(()=>{const td=document.querySelector('tbody tr:not(.hidden) td:nth-child(3)');const s=td.firstElementChild;return [Math.round(s.getBoundingClientRect().right),Math.round(td.getBoundingClientRect().right-parseFloat(getComputedStyle(td).paddingRight))];});
  assert.ok(fit[0]<=fit[1]+1,`a renderCell badge fits its column, padding and all (badge ends ${fit[0]}, cell content ends ${fit[1]})`);
  await page.close();
 }
 // ---- Rows that arrive after an empty first render still fill the card ----
 {
  const code=`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  const [ready, setReady] = React.useState(false);
  React.useEffect(() => { const t = setTimeout(() => setReady(true), 300); return () => clearTimeout(t); }, []);
  return <div className="p-4"><DataTable viz={viz} rows={ready ? viz.rows : []} /></div>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
  const page=await browser.newPage({viewport:{width:1600,height:900}});
  await page.route('**/*',async route=>{const u=new URL(route.request().url());if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'})});
  await page.goto('http://artifact-review.test/');
  await page.addScriptTag({content:'window.exports={};'+compiled});
  await page.evaluate(({code,VIZ,rows,cols})=>{const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[{id:VIZ,title:'T',rows,columns:cols.map(field=>({field}))}]}});const f=document.createElement('iframe');f.style.width='1600px';f.style.height='880px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);},{code,VIZ,rows,cols});
  const frame=page.frames().find(f=>f.parentFrame());
  await frame.waitForFunction(()=>document.querySelector('.bow-dt-scroll table'),null,{timeout:30000});
  await frame.waitForTimeout(500);
  const r=await layout(frame);
  assert.ok(Math.abs(r.table-r.box)<=2,`rows arriving after an empty render still fill the card (${r.table} vs ${r.box})`);
  await page.close();
 }
 // ---- A column named like an Object built-in is just a column ----
 {
  const code=`<script type="text/babel">
function App() {
  const rows = [{ constructor: 'a', toString: 'b', name: 'c' }, { constructor: 'd', toString: 'e', name: 'f' }];
  return <div className="p-4"><DataTable rows={rows} widthKey="proto" /></div>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
  const {page,frame}=await render(browser,1200,code,{proto:{widths:{},order:['toString','hasOwnProperty']}});
  const r=await layout(frame);
  assert.ok(r.widths.every(w=>w>=80&&Number.isFinite(w)),`every column gets a real width (${r.widths})`);
  assert.deepEqual(r.order,['toString','constructor','name'],`the saved order applies; an unknown inherited name adds nothing (${r.order})`);
  await page.close();
 }
 // ---- Opt out ----
 {
  const {page,frame}=await render(browser,1200,app('ltr','autoWidth={false}'));
  const r=await layout(frame);
  assert.ok(!r.fixed&&!r.hasColgroup,'autoWidth={false} keeps the browser layout');
  assert.equal(await frame.locator('[data-testid="bow-dt-resize"]').count(),0,'and has no drag handles');
  // reorderable={false}: a header drag does nothing.
  await page.close();
 }
 {
  const {page,frame}=await render(browser,1200,app('ltr','reorderable={false}'));
  const before=(await layout(frame)).order;
  const a=await frame.locator('th[data-bow-dt-field="Unit price"]').boundingBox();
  await page.mouse.move(a.x+a.width/2,a.y+10); await page.mouse.down();
  for(let i=1;i<=10;i++) await page.mouse.move(a.x+a.width/2-60*i,a.y+10);
  await page.mouse.up(); await frame.waitForTimeout(200);
  assert.deepEqual((await layout(frame)).order,before,'reorderable={false} keeps the order');
  await page.close();
 }
 console.log('PASS: DataTable columns size to content, cut long text on one line, fill the card, and resize by drag (LTR/RTL) and columns move by dragging a header, with the layout kept by the host');
}finally{await browser.close()}
