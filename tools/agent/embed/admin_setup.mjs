// Admin side of the embed loop, through the real UI: sign in, add a host
// application in Settings → Integrations (key shown once), then allow it on a
// dashboard from the dashboard's Settings → Embed tab and read the snippet.
//
//   node tools/agent/embed/admin_setup.mjs <report_id> <out_dir>
// Env: BOW_URL (http://localhost:3000), ADMIN_EMAIL, ADMIN_PASSWORD,
//      ERP_ORIGIN (http://localhost:4173), KEY_OUT (where the private key goes).
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')

const [reportId, out] = process.argv.slice(2)
const BOW = process.env.BOW_URL || 'http://localhost:3000'
const ERP = process.env.ERP_ORIGIN || 'http://localhost:4173'
mkdirSync(out, { recursive: true })

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
// Nuxt UI puts data-testid on the control itself or on its wrapper.
const field = (id) => page.locator(`[data-testid="${id}"]`).locator('xpath=self::input|self::textarea|self::select|.//input|.//textarea|.//select').first()
const shot = (name) => page.screenshot({ path: `${out}/${name}.png` })

page.setDefaultNavigationTimeout(180000); page.setDefaultTimeout(60000)
await page.goto(`${BOW}/users/sign-in`)
await page.fill('#email', process.env.ADMIN_EMAIL)
await page.fill('#password', process.env.ADMIN_PASSWORD)
await page.click('button[type=submit]')
await page.waitForURL((u) => !u.pathname.startsWith('/users/sign-in'), { timeout: 30000 })

// 1. Settings → Integrations → Host applications
await page.goto(`${BOW}/settings/integrations?open=host_apps`)
await page.getByTestId('host-apps').waitFor()
await shot('01-host-apps-empty')
await page.getByTestId('host-app-add').click()
await field('host-app-name').fill('Chinook Back Office')
await field('host-app-origins').fill(ERP)
await field('host-app-issuer').fill('chinook-backoffice')
await field('host-app-source').selectOption('member_attribute:')
await field('host-app-attribute').fill('employee_id')
await shot('02-host-app-form')
await page.getByTestId('host-app-save').click()
const keyBox = page.getByTestId('host-app-private-key')
await keyBox.waitFor()
const privateKey = await keyBox.inputValue()
writeFileSync(process.env.KEY_OUT, privateKey, { mode: 0o600 })
// Never screenshot the key itself: blank it before the capture.
await keyBox.evaluate((el) => { el.value = '-----BEGIN PRIVATE KEY-----\n(redacted for the screenshot)\n-----END PRIVATE KEY-----' })
await shot('03-host-app-key-once')
await page.getByTestId('host-app-key-done').click()
await page.getByTestId('host-app-row').first().waitFor()
await shot('04-host-app-listed')
const publicId = (await page.getByTestId('host-app-row').first().locator('p.font-mono').textContent()).trim()

// 2. The dashboard's Settings → Embed
await page.goto(`${BOW}/reports/${reportId}`)
await page.getByTestId('dashboard-settings').first().click({ timeout: 60000 })
await page.getByTestId('settings-tab-embed').click()
await page.getByTestId('settings-embed').waitFor()
await page.waitForTimeout(500)
await shot('05-embed-tab-before')
await page.getByTestId(`embed-host-${publicId}`).click()
await page.getByTestId('embed-snippet').waitFor()
await page.waitForTimeout(500)
await shot('06-embed-tab-snippet')
const snippet = await page.getByTestId('embed-snippet').textContent()
writeFileSync(`${out}/snippet.html.txt`, snippet)
console.log(JSON.stringify({ publicId }))
await browser.close()
