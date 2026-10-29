// Employee side of the embed loop, like a user: sign in to the back office,
// see the BOW dashboard inside it with no BOW sign-in, as yourself.
//   node tools/agent/embed/erp_flow.mjs <out_dir>
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')

const out = process.argv[2]
const ERP = process.env.ERP_URL || 'http://localhost:4173'
const BOW = process.env.BOW_URL || 'http://localhost:3000'
mkdirSync(out, { recursive: true })
const results = []
const check = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`) }

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })

async function as(user, path = '/', locale = '') {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: locale || 'en-US' })
  const page = await context.newPage()
  page.setDefaultTimeout(90000)
  await page.goto(`${ERP}/signin?user=${user}&next=${encodeURIComponent(path + (locale ? `?locale=${locale}` : ''))}`)
  return { context, page }
}
const frameOf = async (page) => (await page.locator('iframe').first().elementHandle()).contentFrame()

// 1. Alice: the dashboard appears, signed in as her, without a BOW login.
{
  const { context, page } = await as('alice')
  await page.getByTestId('bow-status').filter({ hasText: 'signed in as' }).waitFor()
  const frame = await frameOf(page)
  await frame.locator('[data-embed-status="ready"]').waitFor()
  await frame.locator('iframe').first().waitFor()  // the artifact's own sandbox
  await page.waitForTimeout(6000)                    // charts settle
  check('alice sees the dashboard signed in as herself', (await page.getByTestId('bow-status').textContent()).includes('signed in as alice'), await page.getByTestId('bow-status').textContent())
  check('no BOW top bar inside the host', (await frame.locator('[data-testid="dashboard-topbar"], .h-10.border-b').count()) === 0)
  await page.screenshot({ path: `${out}/10-erp-alice-dashboard.png` })
  // The page's own requests carry only the embed session (memory only).
  const stored = await frame.evaluate(() => JSON.stringify(Object.keys(localStorage)))
  check('embed token is not in the frame storage', !stored.includes('bow_embed'), stored)
  await page.getByTestId('customer-C-25').click()
  await page.waitForTimeout(1000)
  await page.goto(`${ERP}/signin?user=&next=/`)
  await context.close()
}

// 2. Carol: the back office knows her, BOW does not. A clear refusal, no data.
{
  const { context, page } = await as('carol')
  const frame = await (async () => { await page.locator('iframe').first().waitFor(); return frameOf(page) })()
  await frame.locator('[data-embed-status="failed"]').waitFor()
  const msg = await frame.getByTestId('embedded-artifact-message').textContent()
  check('carol (unmapped) is refused with a readable message', /isn't set up|not set up/i.test(msg), msg)
  await page.screenshot({ path: `${out}/11-erp-carol-unmapped.png` })
  await context.close()
}

// 3. Hebrew: the host passes its locale; the embed renders RTL.
{
  const { context, page } = await as('bob', '/', 'he')
  await page.getByTestId('bow-status').filter({ hasText: 'signed in as' }).waitFor()
  const frame = await frameOf(page)
  await frame.locator('[data-embed-status="ready"]').waitFor()
  await page.waitForTimeout(6000)
  check('bob signed in, locale he → dir=rtl', (await frame.evaluate(() => document.documentElement.dir)) === 'rtl')
  await page.screenshot({ path: `${out}/12-erp-bob-he.png` })
  await context.close()
}

// 4. A page on an origin the host application does not list cannot frame it.
{
  const context = await browser.newContext()
  const page = await context.newPage()
  const r = await page.request.get(`${BOW}/d/account-managers?host=${process.env.HOST_APP}`)
  const csp = r.headers()['content-security-policy'] || ''
  check('frame policy names the back office origin only', csp.includes(`frame-ancestors 'self' ${ERP}`) && !csp.includes('*'), csp)
  const plain = await page.request.get(`${BOW}/d/account-managers?host=host_00000000000000000000`)
  check('unknown host: this origin only', (plain.headers()['content-security-policy'] || '').startsWith("frame-ancestors 'self';"), plain.headers()['content-security-policy'])
  await context.close()
}
writeFileSync(`${out}/erp_flow.json`, JSON.stringify(results, null, 1))
await browser.close()
process.exit(results.every(r => r.ok) ? 0 : 1)
