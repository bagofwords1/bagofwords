// Seeded full-stack report, production components, deterministic tool records.
// BOW_TEST_EMAIL=... BOW_TEST_PASSWORD=... node tests/reports/remote-shell-evidence.mjs before|after
import { chromium } from '@playwright/test'
import { mkdirSync, writeFileSync, renameSync, readFileSync } from 'node:fs'
import assert from 'node:assert/strict'
const phase = process.argv[2] || 'after'
const base = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3016'
const api = process.env.BOW_BASE_URL || 'http://127.0.0.1:8016'
const out = process.env.SHOT_DIR || '../media/pr/ssh-tool-lifecycle'
const email = process.env.BOW_TEST_EMAIL, password = process.env.BOW_TEST_PASSWORD
assert(email && password, 'Provide throwaway credentials')
mkdirSync(out, { recursive: true })
const login = await fetch(`${api}/api/auth/jwt/login`, {method:'POST', body:new URLSearchParams({username:email,password})})
assert(login.ok)
const {access_token} = await login.json()
const auth = {Authorization:`Bearer ${access_token}`}
const orgs = await (await fetch(`${api}/api/organizations`, {headers:auth})).json()
const reportResponse = await fetch(`${api}/api/reports`, {method:'POST',headers:{...auth,'X-Organization-Id':orgs[0].id,'Content-Type':'application/json'},body:JSON.stringify({title:'Server maintenance', data_sources:[]})})
assert(reportResponse.ok)
const report = await reportResponse.json()
const browser = await chromium.launch()
const results = []
try {
 for (const locale of ['en','he']) {
  const context = await browser.newContext({viewport:{width:1440,height:1000},recordVideo:locale === 'en' ? {dir:`${out}/video`,size:{width:1440,height:1000}}:undefined})
  await context.addInitScript(l=>localStorage.setItem('bow.locale',l),locale)
  const page = await context.newPage()
  const errors=[]
  page.on('pageerror',e=>errors.push(String(e)))
  await page.goto(`${base}/users/sign-in`, {waitUntil:"domcontentloaded",timeout:90000})
  await page.locator('#email').fill(email)
  await page.locator('#password').fill(password)
  await page.locator('button[type=submit]').click()
  await page.waitForURL(u=>!u.pathname.includes('sign-in'),{timeout:90000})
  if(page.url().includes('/onboarding')) {
   await page.getByRole('button',{name:locale==='he'?/דלג|דילוג/:'Skip onboarding'}).click()
   await page.waitForURL(u=>!u.pathname.includes('/onboarding'))
  }
  await page.goto(`${base}/reports/${report.id}`, {waitUntil:"domcontentloaded",timeout:90000})
  await page.locator('.mention-input-field').first().waitFor({timeout:90000})
  await page.waitForFunction(()=>{
   for(const el of document.querySelectorAll('*')) {
    let c=el.__vueParentComponent
    while(c) {if(c.setupState?.messages && c.setupState?.handleStreamingEvent){window.shellReport=c.setupState;return true} c=c.parent}
   }
   return false
  })
  await page.evaluate(locale=>{
   const s=window.shellReport
   s.stopWatchStream();s.stopPollingInProgressCompletion();s.isStreaming=false;s.isSplitScreen=false
   s.report.data_sources=[{id:'fixture-agent',name:'Operations',connections:[{id:'fixture-ssh',name:'Demo server',type:'remote_ssh'}]}]
   const blocks=[
    {id:'timeout',command:'docker compose up -d',result:{success:false,error_message:'Remote operation timed out; remote shell work may still be running. Check its state before retrying.'},duration_ms:35000},
    {id:'cancelled',command:'docker compose ps',result:{success:false,blocked_by_policy:'ask',approval:{approved:false,timed_out:true},error_message:"Tool 'execute_shell' was not executed because the approval request timed out. Do not retry the same call in this turn; continue the task without it or adjust your approach. If the user explicitly asks for it again later, call it — they will be prompted to approve."},duration_ms:39000},
   ].map((b,i)=>({id:b.id,block_index:i,status:'error',tool_execution:{id:b.id,tool_name:'execute_mcp',status:'error',duration_ms:b.duration_ms,arguments_json:{connection_id:'fixture-ssh',tool_name:'execute_shell',arguments:{command:b.command,working_directory:'/srv/demo'}},result_json:{...b.result,connection_type:'remote_ssh',execution_target:{server:'demo.example',port:22}}}}))
   s.messages=[{id:'fixture-user',role:'user',status:'success',prompt:{content:locale==='he'?'הפעל מחדש ובדוק את מצב השירות.':'Restart the service and check its status.'}},
    {id:'fixture-system',role:'system',status:'stopped',model_name:'Claude Sonnet',prompt:{response_locale:locale},completion_blocks:blocks},
    {id:'fixture-stop-event',role:'event',status:'success',message_type:'run_stopped',prompt:{meta:{completion_id:'fixture-system'},content:'Run was stopped'}}]
  },locale)
  await page.locator('.tool-execution-container').first().waitFor()
  await page.screenshot({path:`${out}/${phase}-${locale}.png`})
  if(phase==='after') {
   const cards=page.getByTestId('remote-shell-tool')
   assert.equal(await cards.count(),2)
   assert.equal(await page.getByTestId('shell-icon').count(),2)
   assert.equal(await page.getByTestId('shell-error').count(),0,'errors hidden until expanded')
   assert.equal(await page.locator('.chat-messages img[alt="MCP"]').count(),0)
   assert(!await page.locator('.chat-messages').innerText().then(t=>t.includes('Do not retry')))
   assert.equal(await page.locator('.chat-messages').getByText('Run was stopped',{exact:false}).count(),0,'duplicate stop event hidden')
   await cards.first().getByRole('button').click()
   await page.getByTestId('shell-details').waitFor()
   assert(await page.getByTestId('shell-details').innerText().then(t=>t.includes('/srv/demo')))
   assert.equal(await page.getByTestId('shell-error').count(),1)
   await page.screenshot({path:`${out}/expanded-${locale}.png`})
   await cards.first().getByRole('button').click()
   // New rows carry their identity without an attached agent (reload / Auto).
   for(const outcome of ['denied','approval_expired','cancelled','success','failed']) {
    await page.evaluate(outcome=>{
     const s=window.shellReport;s.report.data_sources=[]
     s.messages[1].completion_blocks=[{id:`state-${outcome}`,block_index:0,status:'completed',tool_execution:{id:`exec-${outcome}`,tool_name:'execute_mcp',status:outcome==='success'?'success':'error',arguments_json:{tool_name:'execute_shell',arguments:{command:'printf ready',working_directory:'/srv/demo'}},result_json:{connection_type:'remote_ssh',execution_target:{server:'demo.example',port:22},outcome,execution_state:'not_started',success:outcome==='success',error_message:'Fixture detail',preview:{stdout:'ready',stderr:'',exit_code:0}}}}]
    },outcome)
    await page.getByTestId('remote-shell-tool').waitFor()
    assert.equal(await page.getByTestId('shell-error').count(),0)
    await page.getByTestId('remote-shell-tool').getByRole('button').click()
    assert(await page.getByTestId('shell-details').innerText().then(t=>t.includes('demo.example')))
   }
   // Approval controls use the production component and HTTP request shape;
   // the wire reply is deterministic (backend policy paths are tested in pytest).
   let approvalRequest
   await page.route('**/api/completions/fixture-system/mcp_tool_confirmations/fixture-approval', async route=>{
    approvalRequest=route.request().postDataJSON()
    await route.fulfill({json:{approved:true,remembered:false}})
   })
   await page.evaluate(()=>{
    const s=window.shellReport;s.messages[1].status='in_progress'
    s.messages[1].completion_blocks=[{id:'fixture-live',block_index:0,status:'in_progress',tool_execution:{id:'fixture-live-exec',tool_name:'execute_mcp',status:'running',progress_stage:'awaiting_approval',arguments_json:{tool_name:'execute_shell',arguments:{command:'printf ready',working_directory:'/srv/demo'}},result_json:{connection_type:'remote_ssh'},confirmation:{confirmation_id:'fixture-approval',arguments:{command:'printf ready',working_directory:'/srv/demo',_execution_target:{server:'demo.example',shell_permissions:true,mandatory_approval:false}}}}}]
   })
   await page.getByTestId('mcp-approval-card').waitFor()
   await page.screenshot({path:`${out}/approval-${locale}.png`})
   const catalog=JSON.parse(readFileSync(`../locales/${locale}.json`,'utf8'))
   await page.getByRole('button',{name:catalog.tools.mcp.allowOnce,exact:true}).click()
   await page.getByTestId('mcp-approval-card').waitFor({state:'detached'})
   assert.equal(approvalRequest.approved,true)
   assert.equal(approvalRequest.permission_scope,'once')
   await page.evaluate(async()=>{
    const s=window.shellReport
    await s.handleStreamingEvent('tool.progress',{tool_name:'execute_mcp',tool_execution_id:'fixture-live-exec',block_id:'fixture-live',payload:{stage:'connection_resolved',connection_name:'Demo server',connection_type:'remote_ssh',execution_target:{server:'demo.example',port:22}}},1)
    await s.handleStreamingEvent('tool.finished',{tool_name:'execute_mcp',tool_execution_id:'fixture-live-exec',block_id:'fixture-live',status:'success',duration_ms:1200,result_json:{success:true,connection_type:'remote_ssh',execution_target:{server:'demo.example',port:22},preview:{stdout:'ready',stderr:'',exit_code:0},approval:{approved:true}}},1)
    s.messages[1].status='success'
   })
   await page.getByTestId('remote-shell-tool').getByRole('button').click()
   assert(await page.getByTestId('shell-details').innerText().then(t=>t.includes('ready')))
   await page.screenshot({path:`${out}/success-${locale}.png`})
   // Replay a canonical API response on reload, without attached agents.
   const canonical=await page.evaluate(()=>JSON.parse(JSON.stringify(window.shellReport.messages)))
   await page.route(`**/api/reports/${report.id}/completions?*`,route=>route.fulfill({json:{completions:canonical,has_more:false}}))
   await page.reload({waitUntil:'domcontentloaded'})
   await page.getByTestId('remote-shell-tool').waitFor()
   assert.equal(await page.getByTestId('shell-details').count(),0,'rehydrated tool starts collapsed')
   assert.equal(await page.getByTestId('shell-icon').count(),1)
   await page.waitForFunction(()=>{
    for(const el of document.querySelectorAll('*')) {let c=el.__vueParentComponent;while(c){if(c.setupState?.messages&&c.setupState?.handleStreamingEvent){window.shellReport=c.setupState;return true}c=c.parent}}
    return false
   })
   await page.setViewportSize({width:390,height:844})
   assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),'no mobile shell overflow')
   await page.screenshot({path:`${out}/mobile-${locale}.png`})
   // A real MCP tool retains its MCP identity and hides errors until expanded.
   await page.evaluate(()=>{const s=window.shellReport;s.messages[1].completion_blocks=[{id:'mcp-error',block_index:0,status:'error',tool_execution:{id:'mcp-error',tool_name:'execute_mcp',status:'error',arguments_json:{tool_name:'echo'},result_json:{success:false,error_message:'Fixture MCP error'}}}]})
   assert.equal(await page.getByTestId('remote-shell-tool').count(),0)
   assert.equal(await page.getByText('Fixture MCP error',{exact:true}).count(),0)
   await page.locator('.tool-execution-container .cursor-pointer').first().click()
   await page.getByText('Fixture MCP error',{exact:true}).waitFor()
   await page.setViewportSize({width:390,height:844})
   assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),'no mobile overflow')
  }
  results.push({locale,errors})
  const video=page.video()
  await context.close()
  if(video)renameSync(await video.path(),`${out}/${phase}.webm`)
  assert.deepEqual(errors,[],'no browser errors')
 }
 writeFileSync(`${out}/${phase}.json`,JSON.stringify(results,null,2))
 console.log(JSON.stringify({phase,results}))
} finally {await browser.close()}
