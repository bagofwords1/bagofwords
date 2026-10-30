// The editor opens the app's Activity log from the ⋯ menu and filters it to
// errors: the UI over services/artifact_activity.py (ArtifactActivityLog.vue).
//   node tools/agent/embed/activity_log_panel.mjs <report_id> <out_dir>
// Env: BOW_URL (http://localhost:3000), SEED (/tmp/bow-agent/seed_org.json; admin = the editor).
import { createRequire } from 'node:module'
import { mkdirSync, readFileSync } from 'node:fs'
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')
const [reportId, out] = process.argv.slice(2)
const BOW = process.env.BOW_URL || 'http://localhost:3000'
const seed = JSON.parse(readFileSync(process.env.SEED || '/tmp/bow-agent/seed_org.json', 'utf8'))
const en = JSON.parse(readFileSync(new URL('../../../locales/en.json', import.meta.url), 'utf8'))
mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
page.setDefaultTimeout(180000); page.setDefaultNavigationTimeout(180000)
const reads = []
page.on('response', (r) => { if (r.url().includes('/runtime/activity') && r.request().method() === 'GET') reads.push(r.status()) })
await page.goto(`${BOW}/users/sign-in`)
await page.fill('#email', seed.admin.email); await page.fill('#password', seed.admin.password)
await page.click('button[type=submit]'); await page.waitForURL((u) => !u.pathname.startsWith('/users/sign-in'))
await page.goto(`${BOW}/reports/${reportId}`)
await page.waitForSelector('iframe[data-artifact-frame]', { state: 'attached' })
await page.waitForTimeout(4000)
await page.locator(`button[aria-label="${en.artifactFrame.more}"]`).last().click()
await page.getByTestId('activity-log-menu').click()
const panel = page.getByTestId('artifact-activity-log')
await panel.waitFor()
await page.getByTestId('artifact-activity-row').first().waitFor()
await page.waitForTimeout(600)
await page.screenshot({ path: `${out}/94-activity-log.png` })
const all = await page.getByTestId('artifact-activity-row').count()
await page.getByTestId('artifact-activity-errors-only').click()
await page.waitForTimeout(1200)
await page.screenshot({ path: `${out}/95-activity-log-errors-only.png` })
const failed = await page.getByTestId('artifact-activity-row').count()
const onlyFailed = await page.locator('[data-testid=artifact-activity-row][data-failed=false]').count()
console.log(JSON.stringify({ reads, rows: all, errors_only_rows: failed, non_failed_rows_shown_in_errors_only: onlyFailed }))
await browser.close()
if (!reads.length || reads.some((s) => s !== 200) || all === 0 || failed === 0 || onlyFailed !== 0) process.exit(1)
