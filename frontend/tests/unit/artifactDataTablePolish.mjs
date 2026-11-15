// Real Chromium, DataTable:
//  - year and ID columns print as-is (2025, 1001), not "2,025" or a page format's "2.0K";
//    amount_paid and other money columns keep the format;
//  - clickable rows are reachable by keyboard (Tab, Enter) and mark aria-selected;
//  - with rowKey, the selection survives new row objects for the same record;
//  - in an untitled card with its own info icon, the CSV button stays clear of it.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';
const root=new URL('../../',import.meta.url);
const compiled=ts.transpileModule(fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const VIZ='4c3b2a19-8d7e-4f6a-9b0c-1d2e3f4a5b6c';
const rows=[{id:1001,year:2025,revenue:4512.5,amount_paid:4512.5},{id:1002,year:2024,revenue:4775.3,amount_paid:4775.3}];
const app=`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  const [data, setData] = useState(viz.rows);
  const [clicked, setClicked] = useState('');
  const [opened, setOpened] = useState(0);
  return (
    <div className="p-4 space-y-6">
      <div id="fmt"><DataTable viz={viz} rows={data} format={n => fmt(n)} rowKey="id" onRowClick={r => setClicked(String(r.id))} /></div>
      <button id="refresh" onClick={() => setData(data.map(r => ({ ...r })))}>refresh</button>
      <p id="clicked">{clicked}</p>
      <div id="card" style={{ width: 520 }}><SectionCard viz={viz}><DataTable viz={viz} /></SectionCard></div>
      <div id="edit"><DataTable viz={viz} onRowClick={() => setOpened(n => n + 1)} renderCell={(v, r, c) => c.field === 'year' ? <input className="cell-in" defaultValue="" /> : null} /></div>
      <p id="opened">{opened}</p>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true, executablePath:CHROME_PATH});
try{
 const page=await browser.newPage({viewport:{width:900,height:900}});
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
  return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
 });
 await page.goto('http://artifact-review.test/');
 await page.addScriptTag({content:'window.exports={};'+compiled});
 await page.evaluate(({code,VIZ,rows})=>{
  const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[{id:VIZ,title:'Revenue',rows,columns:[{field:'id'},{field:'year'},{field:'revenue'},{field:'amount_paid'}]}]}});
  const f=document.createElement('iframe');f.style.width='900px';f.style.height='880px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
 },{code:app,VIZ,rows});
 const frame=page.frames().find(f=>f.parentFrame());
 await frame.waitForFunction(()=>document.querySelectorAll('#fmt tbody tr').length===2&&document.querySelector('#card [data-testid=bow-dt-export]'),null,{timeout:30000});
 const cells=await frame.$$eval('#fmt tbody tr:first-child td',tds=>tds.map(t=>t.textContent));
 assert.deepEqual(cells.slice(0,2),['1001','2025'],`ID and year print as-is (${cells})`);
 assert.equal(cells[2],'4.5K','other numbers still use the page format');
 assert.equal(cells[3],'4.5K','a money column whose name ends in "paid" is not taken for an ID');
 // keyboard
 await frame.focus('#fmt tbody tr:first-child');
 await page.keyboard.press('Enter');
 assert.equal(await frame.textContent('#clicked'),'1001','Enter on a focused row opens it');
 assert.equal(await frame.getAttribute('#fmt tbody tr:first-child','aria-selected'),'true','the row is marked selected');
 // refresh keeps selection by rowKey
 await frame.click('#refresh');
 await frame.waitForTimeout(200);
 assert.equal(await frame.getAttribute('#fmt tbody tr:first-child','aria-selected'),'true','selection survives new row objects with rowKey');
 // CSV vs card info icon
 const boxes=await frame.evaluate(()=>{
  const r=e=>{const b=e.getBoundingClientRect();return [b.left,b.top,b.right,b.bottom]};
  return {csv:r(document.querySelector('#card [data-testid=bow-dt-export]')),info:r(document.querySelector('#card section > div.absolute'))};
 });
 const overlap=!(boxes.csv[2]<=boxes.info[0]||boxes.info[2]<=boxes.csv[0]||boxes.csv[3]<=boxes.info[1]||boxes.info[3]<=boxes.csv[1]);
 assert.equal(overlap,false,`the CSV button is clear of the card's info icon (${JSON.stringify(boxes)})`);
 // keys typed in a cell's own control stay with the control
 await frame.focus('#edit tbody tr:first-child .cell-in');
 await page.keyboard.press('Space'); await page.keyboard.press('Enter');
 assert.equal(await frame.inputValue('#edit tbody tr:first-child .cell-in'), ' ', 'Space reaches the input in a cell');
 assert.equal(await frame.textContent('#opened'), '0', 'Enter and Space in a cell input do not open the row');
 await frame.focus('#edit tbody tr:first-child'); await page.keyboard.press('Enter');
 assert.equal(await frame.textContent('#opened'), '1', 'Enter on the row itself still opens it');
 console.log('PASS: DataTable years and IDs as-is, keyboard rows, selection by rowKey, CSV clear of the info icon, cell controls keep their keys');
}finally{await browser.close()}
