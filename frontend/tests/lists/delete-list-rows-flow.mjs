// Loop B driver, UI side, for delete_list_rows: log in, open the report, wait
// for the delete confirmation card, screenshot it, then Delete (or Cancel),
// and screenshot the result once the run ends.
//
// usage (from frontend/): PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tests/lists/delete-list-rows-flow.mjs <report_id> <out_dir> [--cancel]
// BOW_CAPTURE_HE=1 also captures the final card in Hebrew and at phone width.
// See docs/feedback-loops/2026-10-21-delete-list-rows-chat-tool.md.
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

const [reportId, outDir, flag] = process.argv.slice(2)
const BASE = process.env.BOW_FRONT || 'http://localhost:3000'
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com'
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!'
fs.mkdirSync(outDir, { recursive: true })

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || undefined })
const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 })
const page = await context.newPage()
page.on('console', m => { if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 200)) })

const lastCard = () => page.locator('[data-testid=delete-list-rows-tool]').last()

try {
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' })
  await page.fill('#email', EMAIL)
  await page.fill('#password', PASSWORD)
  await page.click('button[type=submit]')
  await page.waitForURL(u => !u.pathname.includes('/users/sign-in'), { timeout: 30000 })

  await page.goto(`${BASE}/reports/${reportId}`, { waitUntil: 'domcontentloaded' })
  const confirm = page.locator('[data-testid=delete-list-rows-confirm]').last()
  await confirm.waitFor({ timeout: 180000 })
  await page.waitForTimeout(800)
  await lastCard().screenshot({ path: path.join(outDir, '01-confirm-card.png') })
  await page.screenshot({ path: path.join(outDir, '01-confirm-page.png') })
  console.log('confirm card visible; preview rows:', await page.locator('[data-testid=delete-list-rows-preview] tbody tr').count(),
              'badge:', (await page.locator('[data-testid=delete-list-rows-count]').last().innerText()).trim())

  await confirm.locator(flag === '--cancel' ? '[data-testid=delete-list-rows-cancel]' : '[data-testid=delete-list-rows-approve]').click()
  const end = flag === '--cancel' ? '[data-testid=delete-list-rows-cancelled]' : '[data-testid=delete-list-rows-done]'
  await page.locator(end).last().waitFor({ timeout: 120000 })
  console.log('result:', (await page.locator(end).last().innerText()).trim())
  for (let i = 0; i < 60; i++) {
    if (!(await page.locator('[data-testid=stop-button]').count())) break
    await page.waitForTimeout(2000)
  }
  await page.waitForTimeout(1000)
  await lastCard().screenshot({ path: path.join(outDir, '02-result-card.png') })
  await page.screenshot({ path: path.join(outDir, '03-final-page.png') })

  if (process.env.BOW_CAPTURE_HE === '1') {
    await page.evaluate(() => localStorage.setItem('bow.locale', 'he'))
    await page.reload({ waitUntil: 'domcontentloaded' })
    await lastCard().waitFor({ timeout: 60000 })
    await page.waitForTimeout(1200)
    await lastCard().screenshot({ path: path.join(outDir, '04-card-he.png') })
    await page.evaluate(() => localStorage.removeItem('bow.locale'))
    await page.setViewportSize({ width: 390, height: 844 })
    await page.reload({ waitUntil: 'domcontentloaded' })
    await lastCard().waitFor({ timeout: 60000 })
    await page.waitForTimeout(1200)
    await lastCard().screenshot({ path: path.join(outDir, '05-card-mobile.png') })
    console.log('he + mobile captured')
  }
  console.log('done')
} finally {
  await browser.close()
}
