// Public-browser smoke for the built app. Deterministic completions enter via
// the API response boundary; no Vue dev-mode introspection is used.
import { chromium } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
const base=process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3016'
const api=process.env.BOW_BASE_URL || 'http://127.0.0.1:8016'
const email=process.env.BOW_TEST_EMAIL,password=process.env.BOW_TEST_PASSWORD
assert(email && password,'Provide throwaway credentials')
const phase=process.argv[2] || 'after'
const measurements=[], failures=[]
const out=process.env.SHOT_DIR || '../media/pr/shell-tool-row-style'
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
  page.setDefaultTimeout(90000)
  page.on('pageerror',e=>errors.push(String(e)))
  await page.goto(`${base}/users/sign-in`,{waitUntil:'domcontentloaded'})
  await page.locator('#email').fill(email);await page.locator('#password').fill(password)
  await page.locator('button[type=submit]').click()
  await page.waitForURL(u=>!u.pathname.includes('sign-in'))
  const created_at='2026-01-01T09:00:00Z'
  const completions=['create_data','inspect_data','execute_mcp'].map((tool_name,i)=>({
   id:`reference-${i}`,role:'system',status:'success',created_at,prompt:{response_locale:locale},
   completion_blocks:[{id:`block-${i}`,block_index:0,status:'completed',
    tool_execution:{id:`tool-${i}`,tool_name,status:'success',duration_ms:1000,
     arguments_json:tool_name==='execute_mcp'?{tool_name:'execute_shell',arguments:{command:'docker compose ps',working_directory:'/srv/demo'}}:{},
     result_json:tool_name==='execute_mcp'?{success:true,connection_type:'remote_ssh',execution_target:{server:'demo.example',port:22},preview:{stdout:'ready',exit_code:0}}:{success:true}}}]}))
  await page.route(`**/api/reports/${report.id}/completions?*`,route=>route.fulfill({json:{completions,has_more:false}}))
  await page.goto(`${base}/reports/${report.id}`,{waitUntil:'domcontentloaded'})
  await page.getByTestId('remote-shell-tool').waitFor()
  await page.locator('.chat-messages').evaluate(async el=>Promise.all(el.getAnimations({subtree:true}).filter(a=>Number.isFinite(a.effect?.getComputedTiming().endTime)).map(a=>a.finished.catch(()=>{}))))
  for(const scale of [0.875,0.9375,1,1.125,1.25]) {
   await page.evaluate(value=>document.documentElement.style.setProperty('--bow-chat-text-scale',String(value)),scale)
   const measured=await page.locator('.tool-execution-container').evaluateAll(rows=>{
    const style=el=>{const s=getComputedStyle(el);return Object.fromEntries(['fontSize','fontFamily','fontWeight','lineHeight','letterSpacing'].map(k=>[k,s[k]]))}
    const headers=rows.map(row=>row.querySelector('[aria-expanded]') || row.querySelector('.text-chat-2'))
    return {rowHeights:rows.map(r=>r.getBoundingClientRect().height),headers:headers.map(style),shellLabel:style(rows[2].querySelector('span[dir="ltr"]')),icons:headers.map(h=>h.querySelector('svg, .iconify')?.getBoundingClientRect().width)}
   })
   for(const key of ['fontSize','fontFamily','fontWeight','lineHeight','letterSpacing']) {
    if(measured.shellLabel[key]!==measured.headers[1][key] || measured.headers[0][key]!==measured.headers[1][key])failures.push({locale,scale,key,...measured})
   }
   if(measured.icons[2]!==measured.icons[1])failures.push({locale,scale,key:'iconWidth',...measured})
   if(measured.rowHeights[2]!==measured.rowHeights[1])failures.push({locale,scale,key:'rowHeight',...measured})
   measurements.push({locale,scale,...measured})
   if(scale===0.875)await page.getByTestId('transcript').screenshot({path:`${out}/${phase}-${locale}.png`})
  }
  if(phase==='after') {
   const card=page.getByTestId('remote-shell-tool')
   await page.keyboard.press('Tab')
   await card.getByRole('button').focus()
   const focus=await card.getByRole('button').evaluate(el=>getComputedStyle(el).boxShadow)
   assert.notEqual(focus,'none','keyboard focus stays visible')
   await page.keyboard.press('Enter')
   await page.getByTestId('shell-details').waitFor()
   const mono=await page.getByTestId('shell-details').locator('pre').first().evaluate(el=>getComputedStyle(el).fontFamily)
   assert(mono.includes('mono'),'expanded command keeps monospace')
   await page.keyboard.press('Enter')
   await page.getByTestId('shell-details').waitFor({state:'detached'})
   await page.evaluate(()=>document.documentElement.classList.add('dark'))
   await page.getByTestId('transcript').screenshot({path:`${out}/dark-${locale}.png`})
   await page.setViewportSize({width:390,height:844})
   assert(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))
   await page.screenshot({path:`${out}/mobile-${locale}.png`})
  }
  assert.deepEqual(errors,[])
  await context.close()
 }
} finally {await browser.close()}
writeFileSync(`${out}/${phase}.json`,JSON.stringify({measurements,failures},null,2))
console.log(JSON.stringify({phase,comparisons:measurements.length,failures}))
assert.equal(failures.length,0,'shell typography and icon size match CreateData / InspectData at every reading size')
