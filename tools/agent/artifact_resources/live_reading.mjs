// Held-out prompt qualification. The generated source is never edited.
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const {chromium}=createRequire(new URL('../../../frontend/package.json',import.meta.url))('playwright');
const cases=JSON.parse(await fs.readFile('/tmp/artifact-live-reading.json','utf8'));
const browser=await chromium.launch({headless:true});const results=[];
for(const c of cases){
 const context=await browser.newContext({storageState:'/tmp/artifact-live-browser-state.json',viewport:{width:1440,height:1050}});
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const result={model:c.model,report_id:c.report_id};results.push(result);
 try{
  await page.goto('http://127.0.0.1:3118/reports/'+c.report_id);
  let frame=page.frameLocator('iframe').first();
  const luna=c.model.includes('luna'),title='Accessible document design '+Date.now();
  if(!luna)await frame.getByRole('button',{name:'+ Add article',exact:true}).click();
  await frame.getByLabel('Title',{exact:true}).fill(title);
  await frame.getByLabel('Link',{exact:true}).fill('https://example.com/reading/accessibility');
  await frame.getByRole('button',{name:/Save article/}).click();
  await frame.getByText(title,{exact:true}).waitFor();
  if(luna)await frame.getByRole('checkbox',{name:'Mark as done: '+title,exact:true}).click();
  else await frame.getByRole('button',{name:'Mark done: '+title,exact:true}).click();
  if(luna)await frame.getByRole('checkbox',{name:'Mark as to read: '+title,exact:true}).waitFor();
  await frame.getByRole('button',{name:'Done',exact:true}).first().click();
  await frame.getByText(title,{exact:true}).waitFor();
  await page.reload();frame=page.frameLocator('iframe').first();
  await frame.getByRole('button',{name:'Done',exact:true}).first().click();
  await frame.getByText(title,{exact:true}).waitFor();
  if(luna)assert.equal(await frame.getByRole('checkbox',{name:'Mark as to read: '+title,exact:true}).isChecked(),true);
  else await frame.getByRole('button',{name:'Mark unread: '+title,exact:true}).waitFor();
  assert.deepEqual(errors,[]);
  await page.screenshot({path:`media/pr/artifact-resources/live-${c.model}-reading.png`});
  result.status='passed';console.log(c.model,'PASS: held-out reading app save, mark done, filter and reload');
 }catch(e){result.status='failed';result.error=e.message;await page.screenshot({path:`/tmp/live-${c.model}-reading-failure.png`});console.log(c.model,'FAIL',e.message)}
 finally{await context.close();await fs.writeFile('/tmp/artifact-live-reading-ui.json',JSON.stringify(results,null,2));}
}
await browser.close();assert.ok(results.every(r=>r.status==='passed'));
