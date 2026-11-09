// Public-browser smoke for the built app. Deterministic completions enter via
// the API response boundary; no Vue dev-mode introspection is used.
import { chromium } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
const base=process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3016'
const api=process.env.BOW_BASE_URL || 'http://127.0.0.1:8016'
const email=process.env.BOW_TEST_EMAIL,password=process.env.BOW_TEST_PASSWORD
assert(email && password,'Provide throwaway credentials')
const out=process.env.SHOT_DIR || '../media/pr/ssh-tool-lifecycle'
mkdirSync(out,{recursive:true})
const login=await fetch(`${api}/api/auth/jwt/login`,{method:'POST',body:new URLSearchParams({username:email,password})})
assert(login.ok)
const {access_token}=await login.json(),auth={Authorization:`Bearer ${access_token}`}
const orgs=await (await fetch(`${api}/api/organizations`,{headers:auth})).json()
const response=await fetch(`${api}/api/reports`,{method:'POST',headers:{...auth,'X-Organization-Id':orgs[0].id,'Content-Type':'application/json'},body:JSON.stringify({title:'Shell lifecycle verification',data_sources:[]})})
assert(response.ok)
const report=await response.json()
const browser=await chromium.launch()
try {
 for(const locale of ['en','he']) {
  const context=await browser.newContext({viewport:{width:1440,height:1000}})
  await context.addInitScript(l=>localStorage.setItem('bow.locale',l),locale)
  const page=await context.newPage(),errors=[]
  page.on('pageerror',e=>errors.push(String(e)))
  await page.goto(`${base}/users/sign-in`,{waitUntil:'domcontentloaded'})
  await page.locator('#email').fill(email);await page.locator('#password').fill(password)
  await page.locator('button[type=submit]').click()
  await page.waitForURL(u=>!u.pathname.includes('sign-in'))
  const sid=crypto.randomUUID(),created_at='2026-01-01T09:00:00Z'
  const completions=[{id:crypto.randomUUID(),role:'user',status:'success',created_at,prompt:{content:locale==='he'?'בדוק את מצב השירות.':'Check the service status.'}},
   {id:sid,role:'system',status:'stopped',created_at,prompt:{response_locale:locale},completion_blocks:
    ['execution_timed_out','cancelled'].map((outcome,i)=>({id:crypto.randomUUID(),block_index:i,status:'error',tool_execution:{id:crypto.randomUUID(),tool_name:'execute_mcp',status:'error',duration_ms:i?0:30000,arguments_json:{tool_name:'execute_shell',arguments:{command:i?'docker compose ps':'docker compose up -d',working_directory:'/srv/demo'}},result_json:{success:false,connection_type:'remote_ssh',execution_target:{server:'demo.example',port:22},outcome,execution_state:i?'not_started':'unknown',error_message:i?'Cancelled before execution.':'Remote operation timed out.'}}}))},
   {id:crypto.randomUUID(),role:'event',status:'success',message_type:'run_stopped',created_at,prompt:{meta:{completion_id:sid},content:'Run was stopped'}}]
  await page.route(`**/api/reports/${report.id}/completions?*`,route=>route.fulfill({json:{completions,has_more:false}}))
  await page.goto(`${base}/reports/${report.id}`,{waitUntil:'domcontentloaded'})
  const cards=page.getByTestId('remote-shell-tool')
  await cards.first().waitFor()
  assert.equal(await cards.count(),2)
  assert.equal(await page.getByTestId('shell-icon').count(),2)
  assert.equal(await page.getByTestId('shell-error').count(),0)
  assert.equal(await page.locator('.chat-messages').getByText('Run was stopped',{exact:false}).count(),0)
  await cards.first().getByRole('button').click()
  await page.getByTestId('shell-details').waitFor()
  assert(await page.getByTestId('shell-details').innerText().then(t=>t.includes('demo.example:22')&&t.includes('/srv/demo')))
  assert.equal(await page.getByTestId('shell-error').count(),1)
  await page.screenshot({path:`${out}/production-${locale}.png`})
  await page.reload({waitUntil:'domcontentloaded'})
  await cards.first().waitFor()
  assert.equal(await page.getByTestId('shell-details').count(),0)
  await page.setViewportSize({width:390,height:844})
  assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))
  assert.deepEqual(errors,[])
  console.log(`${locale}: production shell details, cancellation, reload, mobile passed; zero page errors`)
  await context.close()
 }
} finally {await browser.close()}
