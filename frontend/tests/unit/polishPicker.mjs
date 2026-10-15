// Real Chromium: the polish picker on SVG targets and on ⓘ provenance buttons.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import {chromium} from 'playwright';
const root=new URL('../../',import.meta.url);
const source=fs.readFileSync(new URL('utils/artifactIframe.ts',root),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const VIZ='9f407b76-3037-4612-8c97-d3d9f23dba1b';
const code=`<script type="text/babel">
function App() {
  const viz = vizById("${VIZ}");
  return (
    <div className="p-8 space-y-6">
      <div id="card-host"><SectionCard title="Albums card" viz={viz}><p id="card-body">Body</p></SectionCard></div>
      <div className="flex">
        <ul id="hidden" className="h-8 w-16" data-bow-viz="${VIZ}" data-bow-info="off"><li>Hidden</li></ul>
        <ul id="tree" className="h-24 w-64" data-bow-viz="${VIZ}"><li>Tree</li></ul>
        <svg id="spark" width="80" height="20"><path d="M0 10 L80 10" stroke="black" /></svg>
      </div>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>`;
const browser=await chromium.launch({headless:true});
try{
 const page=await browser.newPage();
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.pathname.startsWith('/libs/')){
   const file=new URL('public'+u.pathname,root);
   if(fs.existsSync(file))return route.fulfill({path:file.pathname});
  }
  return route.fulfill({status:200,body:'<html><body></body></html>',contentType:'text/html'});
 });
 await page.goto('http://artifact-review.test/');
 await page.addScriptTag({content:'window.exports={};'+compiled});
 await page.evaluate(({code,VIZ})=>{
  window.msgs=[];addEventListener('message',e=>window.msgs.push(e.data));
  const html=exports.buildArtifactIframeHtml({polishMode:true,code,data:{visualizations:[{id:VIZ,title:'Albums',rows:[{Title:'A'}],columns:[{field:'Title'}]}]}});
  const f=document.createElement('iframe');f.style.width='900px';f.style.height='600px';f.srcdoc=html;document.body.appendChild(f);
 },{code,VIZ});
 const frame=page.frames().find(f=>f.parentFrame());
 await frame.waitForFunction(()=>document.querySelector('#card-host button svg') && document.querySelector('[data-bow-ibtn] svg'));
 const pick=async selector=>{
  await page.evaluate(()=>{window.msgs=[];document.querySelector('iframe').contentWindow.postMessage({type:'POLISH_ENTER'},'*')});
  await frame.waitForFunction(()=>document.body.classList.contains('__polish-active'));
  await frame.evaluate(s=>{const el=document.querySelector(s);el.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}))},selector);
  await page.waitForFunction(()=>window.msgs.some(m=>m.type==='POLISH_ELEMENT_SELECTED'||m.type==='ARTIFACT_ERROR'));
  const errors=await page.evaluate(()=>window.msgs.filter(m=>m.type==='ARTIFACT_ERROR'));
  assert.deepEqual(errors,[],`picking ${selector} must not surface as a dashboard render error`);
  return page.evaluate(()=>window.msgs.find(m=>m.type==='POLISH_ELEMENT_SELECTED').element);
 };
 // ⓘ inside a SectionCard: the pick is the icon itself (highlighted), and the
 // snippet the model edits is its card's code.
 for(const s of ['#card-host button svg','#card-host button svg circle','#card-host button']){
  const el=await pick(s);
  assert.equal(el.tag,'BUTTON',s);
  assert.equal(el.info,true,s);
  assert.equal(el.text,'ⓘ',s);
  const card=await frame.evaluate(()=>document.querySelector('#card-host').firstElementChild.outerHTML.slice(0,40));
  assert.equal(el.htmlSnippet.slice(0,40),card,s);
  assert.match(el.htmlSnippet,/Albums card/,s);
  assert.doesNotMatch(el.classes+el.htmlSnippet,/__polish|data-bow-info-btn|aria-label="Details"/,s);
 }
 // ⓘ drawn by the data-bow-viz overlay (outside the element in the DOM): the
 // snippet is its owner's.
 for(const s of ['[data-bow-ibtn] svg path','[data-bow-ibtn]']){
  const el=await pick(s);
  assert.equal(el.tag,'BUTTON',s);
  assert.equal(el.info,true,s);
  assert.match(el.htmlSnippet,/^<ul id="tree"/,s);
 }
 // A stale but in-range index: an element inserted before the owner shifts
 // document order before the overlay re-renders. The marker still resolves to
 // the element it was drawn for, not to whatever now sits at its index.
 const pickNow=(sel,setup)=>frame.evaluate(({sel,setup})=>{
  (0,eval)(setup)();
  const el=document.querySelector(sel);
  el.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));const lit=!!document.querySelector('.__polish-highlight');
  el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
  return {lit,active:document.body.classList.contains('__polish-active')};
 },{sel,setup});
 const enter=async()=>{await page.evaluate(()=>{window.msgs=[];document.querySelector('iframe').contentWindow.postMessage({type:'POLISH_ENTER'},'*')});await frame.waitForFunction(()=>document.body.classList.contains('__polish-active'))};
 await enter();
 await pickNow('[data-bow-ibtn] svg path',`()=>{const d=document.createElement('div');d.id='intruder';d.setAttribute('data-bow-viz','${VIZ}');d.textContent='Intruder';document.body.prepend(d)}`);
 await page.waitForFunction(()=>window.msgs.some(m=>m.type==='POLISH_ELEMENT_SELECTED'));
 const shifted=await page.evaluate(()=>window.msgs.find(m=>m.type==='POLISH_ELEMENT_SELECTED').element);
 assert.equal(shifted.info,true);
 assert.match(shifted.htmlSnippet,/^<ul id="tree"/,'stale in-range index still resolves to the marker\'s own owner');
 await frame.evaluate(()=>{document.getElementById('intruder').remove();window.dispatchEvent(new Event('resize'))});
 // An owner removed before the overlay re-renders: its marker is not pickable.
 await frame.evaluate(V=>{const d=document.createElement('div');d.id='gone';d.style.cssText='position:fixed;top:320px;left:320px;width:120px;height:40px';d.setAttribute('data-bow-viz',V);document.body.prepend(d);window.dispatchEvent(new Event('resize'))},VIZ);
 await frame.waitForFunction(()=>document.querySelectorAll('[data-bow-ibtn]').length===2);
 await enter();
 const gone=await pickNow('[data-bow-ibtn][data-bow-info-for="0"] svg path',`()=>document.getElementById('gone').remove()`);
 await page.waitForTimeout(300);
 assert.deepEqual(gone,{lit:false,active:true},'detached owner: no highlight, pick mode stays');
 assert.deepEqual(await page.evaluate(()=>window.msgs.filter(m=>m.type==='POLISH_ELEMENT_SELECTED'||m.type==='ARTIFACT_ERROR')),[],'detached owner: nothing sent');
 await page.evaluate(()=>document.querySelector('iframe').contentWindow.postMessage({type:'POLISH_EXIT'},'*'));
 await frame.evaluate(()=>window.dispatchEvent(new Event('resize')));
 await frame.waitForFunction(()=>document.querySelectorAll('[data-bow-ibtn]').length===1);
 // An overlay marker whose owner can't be resolved is not pickable: no
 // highlight, no selection (nothing in the artifact to edit), pick mode stays.
 await frame.evaluate(()=>document.querySelector('[data-bow-ibtn]').removeAttribute('data-bow-info-for'));
 await page.evaluate(()=>{window.msgs=[];document.querySelector('iframe').contentWindow.postMessage({type:'POLISH_ENTER'},'*')});
 await frame.waitForFunction(()=>document.body.classList.contains('__polish-active'));
 const orphan=await frame.evaluate(()=>{const el=document.querySelector('[data-bow-ibtn] svg path');el.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));const lit=!!document.querySelector('.__polish-highlight');el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));return {lit,active:document.body.classList.contains('__polish-active')}});
 await page.waitForTimeout(300);
 assert.deepEqual(orphan,{lit:false,active:true},'unresolved owner: no highlight, pick mode stays');
 assert.deepEqual(await page.evaluate(()=>window.msgs.filter(m=>m.type==='POLISH_ELEMENT_SELECTED'||m.type==='ARTIFACT_ERROR')),[],'unresolved owner: nothing sent');
 await page.evaluate(()=>document.querySelector('iframe').contentWindow.postMessage({type:'POLISH_EXIT'},'*'));
 // Anything else is a regular pick.
 const body=await pick('#card-body');
 assert.notEqual(body.info,true);
 // Any other SVG still picks cleanly (className is an SVGAnimatedString there).
 const spark=await pick('#spark path');
 assert.equal(typeof spark.classes,'string');
 console.log('PASS: ⓘ picks the icon with its owner\'s snippet (inline + overlay); SVG picks never raise a render error');
}finally{await browser.close()}
