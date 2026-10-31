// Inspect, the activity pane under the app (ArtifactActivityLog.vue over
// services/artifact_activity.py): collapsed strip, expanded table, the Errors
// filter, a row's detail, and a live row the moment the app is used.
//   node tools/agent/embed/activity_log_panel.mjs <report_id> <out_dir>
// Env: BOW_URL (http://localhost:3000), SEED (/tmp/bow-agent/seed_org.json; admin = the editor).
import { createRequire } from 'node:module'
import { mkdirSync, readFileSync } from 'node:fs'
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')
const [reportId, out] = process.argv.slice(2)
const BOW = process.env.BOW_URL || 'http://localhost:3000'
const seed = JSON.parse(readFileSync(process.env.SEED || '/tmp/bow-agent/seed_org.json', 'utf8'))
mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
page.setDefaultTimeout(180000); page.setDefaultNavigationTimeout(180000)
const reads = []
page.on('response', (r) => { if (r.url().includes('/runtime/activity') && r.request().method() === 'GET') reads.push(r.status()) })
await page.goto(`${BOW}/users/sign-in`)
await page.fill('#email', seed.admin.email); await page.fill('#password', seed.admin.password)
await page.click('button[type=submit]'); await page.waitForURL((u) => !u.pathname.startsWith('/users/sign-in'))
await page.evaluate(() => localStorage.removeItem('bow.inspect.expanded'))
await page.goto(`${BOW}/reports/${reportId}`)
await page.waitForSelector('iframe[data-artifact-frame]', { state: 'attached' })
// Collapsed by default: the strip with its counts.
const strip = page.getByTestId('artifact-inspect-strip')
await strip.waitFor()
await page.getByTestId('artifact-inspect-strip-errors').waitFor()
await page.waitForTimeout(2500)
const stripText = (await strip.textContent() || '').replace(/\s+/g, ' ').trim()
await page.screenshot({ path: `${out}/93-inspect-collapsed.png` })
await strip.click()
await page.getByTestId('artifact-inspect').waitFor()
await page.getByTestId('artifact-inspect-row').first().waitFor()
await page.waitForTimeout(800)
await page.screenshot({ path: `${out}/94-inspect-pane.png` })
const all = await page.getByTestId('artifact-inspect-row').count()
await page.getByTestId('artifact-inspect-filter-errors').click()
await page.waitForTimeout(500)
const failed = await page.getByTestId('artifact-inspect-row').count()
const nonFailedShown = await page.locator('[data-testid=artifact-inspect-row][data-failed=false]').count()
await page.getByTestId('artifact-inspect-row').first().click()
await page.getByTestId('artifact-inspect-detail').waitFor()
await page.waitForTimeout(400)
await page.screenshot({ path: `${out}/95-inspect-errors-detail.png` })
// Live: using the app adds a row at once, before the batch reaches the server.
await page.getByTestId('artifact-inspect-filter-all').click()
const app = await (await page.locator('iframe[data-artifact-frame]').elementHandle()).contentFrame()
const fresh = app.getByRole('button', { name: /חדשה|new/i })
if (await fresh.count()) await fresh.first().click()
await page.waitForTimeout(1500)
const afterClick = await page.getByTestId('artifact-inspect-row').count()
// Collapse again: the strip is back and remembers nothing else.
await page.getByTestId('artifact-inspect-close').click()
await strip.waitFor()
console.log(JSON.stringify({ strip: stripText, reads, rows: all, errors_rows: failed, non_failed_rows_under_errors: nonFailedShown, rows_after_using_the_app: afterClick }))
await browser.close()
if (!reads.length || reads.some((s) => s !== 200) || all === 0 || failed === 0 || nonFailedShown !== 0 || afterClick <= all) process.exit(1)
