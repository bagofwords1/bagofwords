// UI qualification of the actual generated apps, without editing their source.
// Selectors follow observed generated controls; they are never model input.
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const {chromium}=createRequire(new URL('../../../frontend/package.json',import.meta.url))('playwright');
const cases=JSON.parse(await fs.readFile('/tmp/artifact-live-browser.json','utf8'));
const browser=await chromium.launch({headless:true});
const results=[];
const out='media/pr/artifact-resources';
for(const c of cases){
 const context=await browser.newContext({storageState:'/tmp/artifact-live-browser-state.json',viewport:{width:1440,height:1050}});
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const result={model:c.model,report_id:c.report_id};results.push(result);
 try{
  await page.goto('http://127.0.0.1:3118/reports/'+c.report_id);
  let frame=page.frameLocator('iframe').first();
  await frame.locator('input[type=file]').waitFor({state:'attached'});
  const luna=c.model.includes('luna');
  const fileName=`Field-review-${c.model}-${Date.now()}.txt`;
  const child=page.frames().find(f=>f.parentFrame());
  await child.evaluate(({selector})=>{
   window.__streamObservations=[];
   const observe=()=>{
    const text=document.querySelector(selector)?.textContent||'';
    const busy=[...document.querySelectorAll('button')].some(b=>['Cancel','Stop'].includes(b.textContent.trim()));
    if(text && !/^(Preparing summary|Reading your document|Nothing to summarize)/.test(text)){
     const last=window.__streamObservations.at(-1);
     if(!last||last.length!==text.length||last.busy!==busy) window.__streamObservations.push({length:text.length,busy});
    }
   };
   new MutationObserver(observe).observe(document.body,{subtree:true,childList:true,characterData:true});
  },{selector:luna?'.ds-summary':'.draft'});
  await frame.locator('input[type=file]').setInputFiles({name:fileName,mimeType:'text/plain',buffer:Buffer.from('Project Cedar review. The team agreed to launch the pilot on November 12. Mira owns customer interviews and will complete six interviews by October 28. Omar owns accessibility checks due November 3. The budget is 12000 dollars. The unresolved risk is delayed document approval. Next review is November 5. No personal customer information should be included in the pilot.')});
  if(luna){await frame.getByRole('button',{name:'Upload document',exact:true}).click();await frame.getByRole('button',{name:'Generate summary',exact:true}).click();}
  const content=frame.locator(luna?'.ds-summary':'.draft').first();
  await content.filter({hasText:/Cedar|Mira|November/}).waitFor({timeout:120000});
  result.observedWhileStreaming=await frame.getByRole('button',{name:luna?'Cancel':'Stop',exact:true}).isVisible();
  await page.screenshot({path:`${out}/live-${c.model}-stream.png`});
  const save=frame.getByRole('button',{name:luna?'Keep this summary':'Save this summary',exact:true});
  await save.waitFor({timeout:120000});
  result.streamObservations=await child.evaluate(()=>window.__streamObservations);
  result.observedWhileStreaming ||= result.streamObservations.some(x=>x.busy);
  assert.ok(result.observedWhileStreaming, 'No text was observed before completion');
  assert.ok(new Set(result.streamObservations.filter(x=>x.busy).map(x=>x.length)).size>1,'No incremental text growth observed');
  result.summary=await content.innerText();
  assert.match(result.summary,/Cedar|November|Mira/);
  await save.click();
  await frame.getByText(luna?'Saved to your collection.':'Kept for later in your library.',{exact:true}).waitFor();
  await page.reload();frame=page.frameLocator('iframe').first();
  await frame.getByText(luna?fileName:fileName.replace(/\.txt$/,''),{exact:true}).first().waitFor();
  assert.match(await frame.locator('body').innerText(),/Cedar|November|Mira/);
  await page.screenshot({path:`${out}/live-${c.model}-saved.png`});
  assert.deepEqual(errors,[]);
  result.status='passed';
  console.log(c.model,'PASS: actual generated UI upload, real model stream, save and reload; incremental output:',result.observedWhileStreaming);
 }catch(e){result.status='failed';result.error=e.message;await page.screenshot({path:`/tmp/live-${c.model}-failure.png`});console.log(c.model,'FAIL',e.message)}
 finally{await context.close();await fs.writeFile('/tmp/artifact-live-ui-results.json',JSON.stringify(results,null,2));}
}
await browser.close();
assert.ok(results.every(r=>r.status==='passed'));
