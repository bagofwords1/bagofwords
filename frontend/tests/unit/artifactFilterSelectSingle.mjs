// Real Chromium: a standalone single FilterSelect can go back to "All" (sends []),
// its button names its label for screen readers, and its list stays on screen
// when the control sits at the right edge of a narrow RTL page.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
import {CHROME_PATH} from './support/chromium.mjs';
const root=new URL('../../',import.meta.url);
const source=fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const app=`<script type="text/babel">
function App() {
  const [region, setRegion] = useState('EMEA');
  return (
    <div dir="rtl" className="p-2">
      <FilterSelect label="Region" single placeholder="All regions" options={['EMEA','APAC','AMER']}
        selected={region ? [region] : []} onChange={a => { window.__last = a; setRegion(a[0] ?? null); }} />
      <p id="state">{region ?? 'ALL'}</p>
      <div id="full"><FilterSelect label="Full" wrapperClassName="w-full" options={['One','Two']} selected={[]} onChange={() => {}} /></div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true, executablePath:CHROME_PATH});
try{
 const page=await browser.newPage({viewport:{width:390,height:700}});
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.pathname.startsWith('/libs/')){const file=new URL('public'+u.pathname,root);if(fs.existsSync(file))return route.fulfill({path:file.pathname})}
  return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
 });
 await page.goto('http://artifact-review.test/');
 await page.addScriptTag({content:'window.exports={};'+compiled});
 await page.evaluate(({code})=>{
  const html=exports.buildArtifactIframeHtml({code,data:{runtime:{version:11},visualizations:[]}});
  const f=document.createElement('iframe');f.style.width='390px';f.style.height='680px';f.style.border='0';f.srcdoc=html;document.body.appendChild(f);
 },{code:app});
 const frame=page.frames().find(f=>f.parentFrame());
 await frame.waitForSelector('button[aria-haspopup="listbox"]',{timeout:30000});
 assert.equal(await frame.getAttribute('button[aria-haspopup="listbox"]','aria-label'),'Region: EMEA','the button names its label and value');
 // A full-width control in an unpadded page: the list stays inside the viewport.
 await frame.evaluate(()=>{document.body.style.margin='0';document.querySelector('#full').parentElement.style.padding='0'});
 await frame.click('#full button[aria-haspopup="listbox"]');
 const full=await (await frame.waitForSelector('body > div[style*="position: fixed"]')).evaluate(el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,vw:window.innerWidth}});
 assert.ok(full.left>=0&&full.right<=full.vw+0.5,`a full-width list stays on screen (${full.left}..${full.right} of ${full.vw})`);
 await frame.click('#full button[aria-haspopup="listbox"]');
 await frame.waitForFunction(()=>!document.querySelector('body > div[style*="position: fixed"]'));
 await frame.click('button[aria-haspopup="listbox"]');
 const list=await frame.waitForSelector('body > div[style*="position: fixed"]');
 const box=await list.evaluate(el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,vw:window.innerWidth}});
 assert.ok(box.left>=0&&box.right<=box.vw,`the list stays on screen (${box.left}..${box.right} of ${box.vw})`);
 await frame.click('text=All regions');
 assert.equal(await frame.textContent('#state'),'ALL','choosing All clears the selection');
 assert.deepEqual(await frame.evaluate(()=>window.__last),[],'onChange receives []');
 console.log('PASS: single FilterSelect clears to All, is labelled, and stays on screen');
}finally{await browser.close()}
