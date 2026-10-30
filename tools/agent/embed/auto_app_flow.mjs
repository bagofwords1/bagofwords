// An app built from an Auto report (no agents attached), used in the back
// office by an employee, in Hebrew: the answer must come from the data
// through an agent, never "I only have the dashboard's data".
//   node tools/agent/embed/auto_app_flow.mjs <out_dir> "<question>"
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')

const [out, question] = process.argv.slice(2)
const ERP = process.env.ERP_URL || 'http://localhost:4173'
mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
page.setDefaultTimeout(240000)
const failed = []
page.on('response', (r) => { if (r.url().includes('/runtime/conversations') && r.status() >= 400) failed.push(`${r.status()} ${r.url()}`) })
await page.goto(`${ERP}/signin?user=alice&next=/assistant?locale=he`)
await page.getByTestId('bow-status').filter({ hasText: 'signed in as' }).waitFor()
const bow = await (await page.locator('iframe').first().elementHandle()).contentFrame()
await bow.locator('iframe').first().waitFor()
const app = await (await bow.locator('iframe').first().elementHandle()).contentFrame()
const box = app.locator('textarea, input[type=text]').last()
await box.waitFor()
await page.waitForTimeout(2000)
await box.fill(question)
await box.press('Enter')
let last = '', stable = Date.now(), t0 = Date.now()
while (Date.now() - t0 < 300000) {
  await page.waitForTimeout(1000)
  const text = await app.locator('body').innerText()
  if (text !== last) { last = text; stable = Date.now(); continue }
  if (text.lastIndexOf(question) >= 0 && text.slice(text.lastIndexOf(question)).length > question.length + 120 && Date.now() - stable > 8000) break
}
await page.screenshot({ path: `${out}/50-auto-app-he-answer.png` })
const answer = last.slice(last.lastIndexOf(question)).replace(/\s+/g, ' ')
const noAccess = /אין לי גישה|no access|dashboard data only|create_data/i.test(answer)
const result = { failedRequests: failed, answer: answer.slice(0, 600), saysNoAccess: noAccess }
writeFileSync(`${out}/auto_app_flow.json`, JSON.stringify(result, null, 1))
console.log(JSON.stringify(result, null, 1))
await browser.close()
process.exit(failed.length === 0 && !noAccess && answer.length > question.length + 120 ? 0 : 1)
