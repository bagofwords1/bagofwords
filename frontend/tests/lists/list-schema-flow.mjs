// Loop B driver, UI side: log in, open the training report, screenshot the
// review card, approve it, screenshot the result, then wait for the run to
// finish and screenshot the whole conversation (submit_<slug> card included).
//
// usage (from frontend/): PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tests/lists/list-schema-flow.mjs <report_id> <out_dir> [--decline "feedback"]
// BOW_CAPTURE_HE=1 also captures the final card in Hebrew and at phone width.
// See docs/feedback-loops/2026-10-15-create-list-edit-list-training-tools.md.
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

const [reportId, outDir, flag, feedbackText] = process.argv.slice(2)
const BASE = process.env.BOW_FRONT || 'http://localhost:3000'
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com'
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!'
fs.mkdirSync(outDir, { recursive: true })

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || undefined })
const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 })
const page = await context.newPage()
page.on('console', m => { if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 200)) })

try {
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' })
  await page.fill('#email', EMAIL)
  await page.fill('#password', PASSWORD)
  await page.click('button[type=submit]')
  await page.waitForURL(u => !u.pathname.includes('/users/sign-in'), { timeout: 30000 })

  await page.goto(`${BASE}/reports/${reportId}`, { waitUntil: 'domcontentloaded' })
  const card = page.locator('[data-testid=list-schema-card]').last()
  await card.waitFor({ timeout: 120000 })
  // The review controls appear once the confirmation event lands (before that the
  // card shows the call's arguments); wait for them so the shot is the review state.
  await card.locator('[data-testid=list-schema-review]').waitFor({ timeout: 120000 }).catch(() => {})
  await page.waitForTimeout(800)
  await card.screenshot({ path: path.join(outDir, '01-review-card.png') })
  await page.screenshot({ path: path.join(outDir, '01-review-page.png') })
  console.log('review card visible; fields:', await page.locator('[data-testid=list-schema-fields] li').count())

  if (flag === '--decline') {
    await card.locator('[data-testid=list-schema-request-changes]').click()
    await card.locator('[data-testid=list-schema-feedback]').fill(feedbackText || 'please change it')
    await page.screenshot({ path: path.join(outDir, '02-feedback.png') })
    await card.locator('[data-testid=list-schema-send-feedback]').click()
  } else {
    await card.locator('[data-testid=list-schema-approve]').click()
  }
  // The agent resumes: wait for the saved card (created/updated) …
  if (flag !== '--decline') {
    await card.locator('[data-testid=list-schema-result]').waitFor({ timeout: 120000 })
    await page.waitForTimeout(500)
    await card.screenshot({ path: path.join(outDir, '02-created-card.png') })
    console.log('created card visible')
  }
  // … and for the run to end (stop button disappears / submit card shows).
  const submit = page.locator('[data-testid=submit-list-tool]').last()
  try { await submit.waitFor({ timeout: 240000 }) ; console.log('submit card visible') } catch { console.log('no submit card within budget') }
  // give the final answer a moment to stream
  for (let i = 0; i < 60; i++) {
    const running = await page.locator('[data-testid=stop-button]').count()
    if (!running) break
    await page.waitForTimeout(2000)
  }
  await page.waitForTimeout(1000)
  await page.screenshot({ path: path.join(outDir, '03-final-page.png'), fullPage: false })
  const body = page.locator('[data-testid=list-schema-tool]').last()
  await body.screenshot({ path: path.join(outDir, '03-final-list-card.png') }).catch(() => {})
  if (await submit.count()) {
    await submit.locator('[data-testid=submit-list-header]').click().catch(() => {})
    await page.waitForTimeout(500)
    await submit.screenshot({ path: path.join(outDir, '04-submit-card.png') }).catch(() => {})
  }
  if (process.env.BOW_CAPTURE_HE === '1') {
    await page.evaluate(() => localStorage.setItem('bow.locale', 'he'))
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('[data-testid=list-schema-card]').last().waitFor({ timeout: 60000 })
    await page.waitForTimeout(1200)
    await page.locator('[data-testid=list-schema-card]').last().screenshot({ path: path.join(outDir, '05-card-he.png') })
    await page.evaluate(() => localStorage.removeItem('bow.locale'))
    await page.setViewportSize({ width: 390, height: 844 })
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('[data-testid=list-schema-card]').last().waitFor({ timeout: 60000 })
    await page.waitForTimeout(1200)
    await page.locator('[data-testid=list-schema-card]').last().screenshot({ path: path.join(outDir, '06-card-mobile.png') })
    console.log('he + mobile captured')
  }
  console.log('done')
} finally {
  await browser.close()
}
