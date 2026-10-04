// Deterministic real-UI SSE regression; uses a seeded report, no model calls.
import {createRequire} from 'node:module';import fs from 'node:fs';
const require=createRequire(new URL('../../frontend/package.json', import.meta.url));const {chromium}=require('@playwright/test');
const [baseURL,reportId,label='after',out='media/pr/stream-message-identity']=process.argv.slice(2);
if(!baseURL || !reportId || !process.env.BOW_TEST_EMAIL || !process.env.BOW_TEST_PASSWORD)
 throw new Error('Usage: BOW_TEST_EMAIL=... BOW_TEST_PASSWORD=... node tools/agent/verify_stream_identity.mjs URL REPORT_ID [before|after] [output-dir]');
fs.mkdirSync(out,{recursive:true});
const browser=await chromium.launch({headless:true});const context=await browser.newContext({viewport:{width:1440,height:960},recordVideo:{dir:out,size:{width:1440,height:960}}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(String(e)));
try {
 await page.goto(`${baseURL}/users/sign-in`);await page.locator('input[type="text"],input[type="email"]').first().fill(process.env.BOW_TEST_EMAIL);await page.locator('input[type="password"]').fill(process.env.BOW_TEST_PASSWORD);await page.locator('button[type="submit"]').click();await page.waitForURL(u=>!u.pathname.includes('sign-in'),{timeout:60000});
 await page.goto(`${baseURL}/reports/${reportId}`);await page.locator('.mention-input-field').first().waitFor({timeout:90000});
 await page.evaluate(()=>{for(const e of document.querySelectorAll('*')){let c=e.__vueParentComponent;while(c){if(c.setupState?.startStreaming){window.__report=c.setupState;break}c=c.parent}if(window.__report)break}});
 await page.evaluate(()=>{
  const s=window.__report;s.stopWatchStream();s.stopPollingInProgressCompletion();
  const original=window.fetch.bind(window);window.fetch=(url,opts)=>{
   if(String(url).includes('/completions')&&opts?.method==='POST')return Promise.resolve(new Response(new ReadableStream({start(c){window.__sse=c}}),{headers:{'Content-Type':'text/event-stream'}}));
   return original(url,opts);
  };
  s.messages=[{id:'user-evidence',role:'user',prompt:{content:'Show the Chinook revenue summary.'},created_at:new Date().toISOString(),status:'success'}, {id:'system-evidence',role:'system',status:'in_progress',created_at:new Date().toISOString(),completion_blocks:[]}];
  s.currentController=new AbortController();s.isStreaming=true;s.isCompletionInProgress=true;
  window.__run=s.startStreaming({prompt:{content:'Stream identity regression'},stream:true},'system-evidence');
  window.__emit=(event,data)=>window.__sse.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify({data})}\n\n`));
 });
 await page.waitForFunction(()=>!!window.__sse);
 await page.evaluate(()=>window.__emit('completion.started',{system_completion_id:'saved-evidence'}));
 await page.waitForFunction(()=>window.__report.messages.at(-1).system_completion_id==='saved-evidence');
 await page.waitForTimeout(700);
 await page.evaluate(()=>{const s=window.__report;s.messages=[s.messages[0],{id:'saved-evidence',role:'system',status:'in_progress',created_at:new Date().toISOString(),completion_blocks:[]}];});
 await page.evaluate(()=>{
  window.__emit('block.upsert',{block:{id:'answer-evidence',block_index:0,phase:'final_answer',status:'success',content:'Chinook revenue totals $1,428.56 for 2022–2024.'}});
  window.__emit('completion.finished',{status:'success'});
 });
 await page.waitForTimeout(1000);
 const result=await page.evaluate(()=>({status:window.__report.messages.at(-1).status,blocks:window.__report.messages.at(-1).completion_blocks}));
 await page.screenshot({path:`${out}/${label}.png`});
 console.log(JSON.stringify({label,...result,errors}));
 fs.writeFileSync(`${out}/${label}.json`,JSON.stringify({label,...result,errors},null,2));
 if(errors.length || result.status !== (label==='before'?'in_progress':'success')) throw new Error('Unexpected browser result');
 await page.evaluate(()=>window.__sse.close());await page.waitForTimeout(500);
} finally {await context.close();const video=await page.video().path();fs.renameSync(video,`${out}/${label}.webm`);await browser.close();}
