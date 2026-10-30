// The embed session's lifecycle inside the back office, in a real browser:
//  1. silent renewal: an SDK call works before AND after the token is renewed
//     (the app's channel must survive it);
//  2. logout during sign-in: the host logs out while its assertion is still on
//     the way; the late session must not sign the user back in, and is revoked.
// Run the backend with a short session (renewal ~20 s after sign-in), e.g.
// /tmp/bow-agent/short_session_main.py in the sandbox.
//   node tools/agent/embed/session_lifecycle_flow.mjs <out_dir>
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')

const out = process.argv[2]
const ERP = process.env.ERP_URL || 'http://localhost:4173'
mkdirSync(out, { recursive: true })
const results = []
const check = (name, ok, detail = '') => { results.push({ name, ok, detail: String(detail).slice(0, 300) }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`) }
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })

async function frames(page) {
  const bow = await (await page.locator('iframe').first().elementHandle()).contentFrame()
  await bow.locator('iframe').first().waitFor()
  const app = await (await bow.locator('iframe').first().elementHandle()).contentFrame()
  return { bow, app }
}
const conversations = (app) => app.evaluate(() => window.bow.conversations.list({ limit: 50 }).then(r => r.items.length))
const createOne = (app) => app.evaluate(() => Promise.race([
  window.bow.conversations.create({ title: 'renewal check' }).then(() => 'ok', e => 'error ' + e.code),
  new Promise(r => setTimeout(() => r('timeout'), 15000)),
]))

// 1. renewal keeps the app connected
{
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 860 } })).newPage()
  page.setDefaultTimeout(120000)
  const sessions = []
  page.on('request', r => { if (/\/api\/embed\/[^/]+\/session$/.test(r.url()) && r.method() === 'POST') sessions.push(Date.now()) })
  await page.goto(`${ERP}/signin?user=alice&next=/assistant`)
  await page.getByTestId('bow-status').filter({ hasText: 'signed in as' }).waitFor()
  const { app } = await frames(page)
  await app.locator('textarea').waitFor()
  const before = await createOne(app)
  check('SDK call before renewal', before === 'ok', before)
  const t0 = Date.now()
  while (sessions.length < 2 && Date.now() - t0 < 90000) await page.waitForTimeout(1000)
  check('session renewed silently', sessions.length >= 2, `${sessions.length} sign-ins`)
  await page.waitForTimeout(1500)
  const after = await createOne(app)
  check('SDK call after renewal', after === 'ok', after)
  const listed = await Promise.race([conversations(app), new Promise(r => setTimeout(() => r('timeout'), 15000))])
  check('app still lists conversations', typeof listed === 'number', listed)
  await page.screenshot({ path: `${out}/80-after-renewal.png` })
  await page.context().close()
}

// 2. logout while the host's assertion is still on the way
{
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 860 } })).newPage()
  page.setDefaultTimeout(60000)
  let deletes = 0, posts = 0
  page.on('request', r => {
    if (!/\/api\/embed\/[^/]+\/session$/.test(r.url())) return
    if (r.method() === 'DELETE') deletes++
    if (r.method() === 'POST') posts++
  })
  await page.goto(`${ERP}/signin?user=alice&next=/assistant`)
  await page.route('**/bow/assertion', async (route) => { await new Promise(r => setTimeout(r, 4000)); await route.continue() })
  await page.reload()
  await page.waitForFunction(() => !!window.embed)
  await page.waitForTimeout(1500)            // the frame has asked; the assertion is delayed
  await page.evaluate(() => window.embed.logout())
  await page.waitForTimeout(7000)            // the late assertion arrives and is exchanged
  const bow = await (await page.locator('iframe').first().elementHandle()).contentFrame()
  const status = await bow.locator('[data-testid=embedded-artifact]').getAttribute('data-embed-status')
  check('logout wins over a late sign-in', status === 'signed_out', `status=${status}`)
  check('the app is not shown after logout', (await bow.locator('iframe').count()) === 0)
  check('a late session is revoked', posts === 0 || deletes >= posts, `posts=${posts} deletes=${deletes}`)
  await page.screenshot({ path: `${out}/81-logout-during-sign-in.png` })
  await page.context().close()
}

writeFileSync(`${out}/session_lifecycle_flow.json`, JSON.stringify(results, null, 1))
await browser.close()
process.exit(results.every(r => r.ok) ? 0 : 1)
