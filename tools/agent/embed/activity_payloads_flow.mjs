// Inspect shows what a call carried: the editor sends a message in their chat
// app, the stream row appears live, and its Request / Stream tabs hold the
// text and the events (ArtifactActivityLog.vue over
// services/artifact_activity.py payloads). Then the same row from history.
//   node tools/agent/embed/activity_payloads_flow.mjs <report_id> <out_dir> ["<question>"]
// Env: BOW_URL (http://localhost:3000), SEED (/tmp/bow-agent/seed_org.json; admin = the editor).
import { createRequire } from 'node:module'
import { mkdirSync, readFileSync } from 'node:fs'
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')
const [reportId, out, question = 'how many artists are there?'] = process.argv.slice(2)
const BOW = process.env.BOW_URL || 'http://localhost:3000'
const seed = JSON.parse(readFileSync(process.env.SEED || '/tmp/bow-agent/seed_org.json', 'utf8'))
const en = JSON.parse(readFileSync(new URL('../../../locales/en.json', import.meta.url), 'utf8'))
mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
page.setDefaultTimeout(180000); page.setDefaultNavigationTimeout(180000)
const posted = []
page.on('request', (r) => { if (r.url().includes('/runtime/activity') && r.method() === 'POST') posted.push(r.postDataJSON()) })
await page.goto(`${BOW}/users/sign-in`)
await page.fill('#email', seed.admin.email); await page.fill('#password', seed.admin.password)
await page.click('button[type=submit]'); await page.waitForURL((u) => !u.pathname.startsWith('/users/sign-in'))
await page.goto(`${BOW}/reports/${reportId}`)
const handle = await page.waitForSelector('iframe[data-artifact-frame]', { state: 'attached' })
const app = await handle.contentFrame()
await page.waitForTimeout(4000)
await page.locator(`button[aria-label="${en.artifactFrame.more}"]`).last().click()
await page.getByTestId('inspect-menu').click()
await page.getByTestId('artifact-inspect').waitFor()
// Use the app: a message streams an answer; Inspect shows the row at once.
const fresh = app.getByRole('button', { name: /חדשה|new/i })
if (await fresh.count()) { await fresh.first().click(); await page.waitForTimeout(1200) }
const box = app.locator('textarea, input[type=text]').last()
await box.click(); await box.fill(question); await box.press('Enter')
const streamRow = page.locator('[data-testid=artifact-inspect-row][data-kind=stream]').first()
await streamRow.waitFor({ timeout: 120000 })
await page.waitForTimeout(Number(process.env.WAIT_MS || 8000))
await page.getByTestId('artifact-inspect-filter-stream').click()
await page.locator('[data-testid=artifact-inspect-row][data-kind=stream]').first().click()
await page.getByTestId('artifact-inspect-tab-stream').click()
await page.getByTestId('artifact-inspect-stream').waitFor()
const liveEvents = await page.locator('[data-testid=artifact-inspect-stream] li').count()
await page.screenshot({ path: `${out}/96-inspect-stream-live.png` })
await page.getByTestId('artifact-inspect-tab-request').click()
const liveRequest = await page.getByTestId('artifact-inspect-request').textContent()
// The same row from the server: refresh, open it again, the payload is fetched.
await page.getByTestId('artifact-inspect-refresh').click()
await page.waitForTimeout(3000)
const stored = page.locator('[data-testid=artifact-inspect-row][data-kind=stream]').first()
await stored.click()
await page.waitForTimeout(1500)
if (!(await page.getByTestId('artifact-inspect-detail').count())) await stored.click()  // a click on an open row closes it
await page.getByTestId('artifact-inspect-tab-stream').waitFor()
await page.getByTestId('artifact-inspect-tab-stream').click()
await page.getByTestId('artifact-inspect-stream').waitFor()
const storedEvents = await page.locator('[data-testid=artifact-inspect-stream] li').count()
await page.getByTestId('artifact-inspect-tab-request').click()
const storedRequest = await page.getByTestId('artifact-inspect-request').textContent()
await page.screenshot({ path: `${out}/97-inspect-stream-stored.png` })
// A plain request's response (conversations.create / messages): the Response tab.
await page.getByTestId('artifact-inspect-filter-request').click()
const req = page.locator('[data-testid=artifact-inspect-row][data-kind=request]').first()
await req.click()
await page.waitForTimeout(1500)
if (!(await page.getByTestId('artifact-inspect-detail').count())) await req.click()
await page.getByTestId('artifact-inspect-tab-response').waitFor()
await page.getByTestId('artifact-inspect-tab-response').click()
const response = await page.getByTestId('artifact-inspect-response').textContent()
await page.screenshot({ path: `${out}/98-inspect-response.png` })
const sentPayloads = posted.flatMap((b) => b.events || []).filter((e) => e.payload).length
console.log(JSON.stringify({ live_stream_events: liveEvents, stored_stream_events: storedEvents,
  request_is_the_send: /conversationSend/.test(liveRequest || '') && /"text"/.test(liveRequest || '') && /conversationSend/.test(storedRequest || ''),
  response_chars: (response || '').length, batches_with_payloads: sentPayloads }))
await browser.close()
if (!liveEvents || !storedEvents || !/conversationSend/.test(liveRequest || '') || !/conversationSend/.test(storedRequest || '') || !sentPayloads) process.exit(1)
