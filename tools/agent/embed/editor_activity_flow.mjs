// The editor uses their own app in BOW and it fails: what happened must land
// in the app's activity log (services/artifact_activity.py) without anyone
// pasting errors -- the request, its error code, and the clicks before it.
//   node tools/agent/embed/editor_activity_flow.mjs <report_id> <out_dir> "<question>"
// Env: BOW_URL (http://localhost:3000), seed file /tmp/bow-agent/seed_org.json (admin = the editor).
import { createRequire } from 'node:module'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')

const [reportId, out, question] = process.argv.slice(2)
const BOW = process.env.BOW_URL || 'http://localhost:3000'
const seed = JSON.parse(readFileSync(process.env.SEED || '/tmp/bow-agent/seed_org.json', 'utf8'))
mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
page.setDefaultTimeout(180000); page.setDefaultNavigationTimeout(180000)
const posted = []
page.on('request', (r) => { if (r.url().includes('/runtime/activity')) posted.push(r.postDataJSON()) })

await page.goto(`${BOW}/users/sign-in`)
await page.fill('#email', seed.admin.email); await page.fill('#password', seed.admin.password)
await page.click('button[type=submit]'); await page.waitForURL((u) => !u.pathname.startsWith('/users/sign-in'))
await page.goto(`${BOW}/reports/${reportId}`)
const handle = await page.waitForSelector('iframe[data-artifact-frame]', { state: 'attached' })
const app = await handle.contentFrame()
const box = app.locator('textarea, input[type=text]').last()
await box.waitFor()
await page.waitForTimeout(2500)
// What a person does first: start a new conversation (a click breadcrumb).
const fresh = app.getByRole('button', { name: /חדשה|new/i })
if (await fresh.count()) { await fresh.first().click(); await page.waitForTimeout(1500) }
await box.click()
await box.fill(question)
await box.press('Enter')
const send = app.getByRole('button', { name: /שלח|send/i })
if (await send.count()) await send.first().click().catch(() => {})
await page.waitForTimeout(Number(process.env.WAIT_MS || 6000))
await page.screenshot({ path: `${out}/${process.env.EXPECT === 'ok' ? '71-editor-app-fixed' : '70-editor-app-fails'}.png` })
const events = posted.flatMap((b) => b.events || [])
const failed = events.filter((e) => e.status === 'error' || e.kind === 'error')
const result = { batches: posted.length, events: events.map((e) => [e.kind, e.op, e.status, e.error_code, e.detail?.target].filter(Boolean).join(' ')), failed: failed.length }
writeFileSync(`${out}/editor_activity_flow.json`, JSON.stringify(result, null, 1))
console.log(JSON.stringify(result, null, 1))
await browser.close()
// EXPECT=ok: a working app (no failures); default: a broken one (failures recorded).
process.exit((process.env.EXPECT === "ok" ? failed.length === 0 && events.length > 0 : failed.length > 0) ? 0 : 1)
