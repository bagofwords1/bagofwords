// SSO-mode host application, like a user: the embedded page offers "Sign in",
// BOW's own sign-in runs in a popup, the dashboard appears. No host backend.
//   node tools/agent/embed/sso_flow.mjs <out_dir>   (env MEMBER_EMAIL, MEMBER_PASSWORD)
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')

const out = process.argv[2]
const ERP = process.env.ERP_URL || 'http://localhost:4173'
mkdirSync(out, { recursive: true })
const results = []
const check = (name, ok, detail = '') => { results.push({ name, ok, detail: String(detail).slice(0, 300) }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${String(detail).slice(0, 200)}`) }

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
page.setDefaultTimeout(120000)
await page.goto(`${ERP}/signin?user=alice&next=/portal`)
await page.locator('iframe').first().waitFor()
const frame = await (await page.locator('iframe').first().elementHandle()).contentFrame()
await frame.locator('[data-embed-status="sign_in_required"]').waitFor()
check('the frame asks the person to sign in (nothing silent is possible)', true)
await page.screenshot({ path: `${out}/30-sso-sign-in-required.png` })

const [popup] = await Promise.all([context.waitForEvent('page'), frame.getByTestId('embedded-artifact-sign-in').click()])
await popup.waitForURL(/users\/sign-in/)
check('the popup is BOW sign-in on the BOW origin', popup.url().startsWith('http://localhost:3000/users/sign-in'), popup.url())
await popup.fill('#email', process.env.MEMBER_EMAIL)
await popup.fill('#password', process.env.MEMBER_PASSWORD)
await popup.screenshot({ path: `${out}/31-sso-popup.png` })
await popup.click('button[type=submit]')
await popup.waitForEvent('close', { timeout: 120000 })
check('the popup closes itself after sign-in', true)
await frame.locator('[data-embed-status="ready"]').waitFor()
await page.getByTestId('bow-status').filter({ hasText: 'signed in as' }).waitFor()
await page.waitForTimeout(6000)
check('the dashboard appears signed in as the person', (await page.getByTestId('bow-status').textContent()).includes('alice'))
await page.screenshot({ path: `${out}/32-sso-dashboard.png` })

writeFileSync(`${out}/sso_flow.json`, JSON.stringify(results, null, 1))
await browser.close()
process.exit(results.every(r => r.ok) ? 0 : 1)
