// UI evidence for 2026-10-31-instruction-application-scope.md.
// Opens an instruction in /agents, expands Advanced, opens the Channels
// multi-select and (in "after" mode) picks two connected apps, then reloads
// to show the choice persisted.
//
//   cd frontend && node ../docs/feedback-loops/2026-10-31-instruction-application-scope.ui.mjs \
//       <instruction_id> <out_dir> <before|after> "<app name 1>" "<app name 2>"
import { createRequire } from 'node:module'
const { chromium } = createRequire(process.cwd() + '/')('playwright')
import fs from 'node:fs'

const [id, out, mode, app1, app2] = process.argv.slice(2)
fs.mkdirSync(out, { recursive: true })
const browser = await chromium.launch(fs.existsSync('/opt/pw-browsers/chromium') ? { executablePath: '/opt/pw-browsers/chromium' } : {})
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.setDefaultNavigationTimeout(180000)
page.setDefaultTimeout(90000)
await page.goto('http://localhost:3000/users/sign-in', { waitUntil: 'domcontentloaded' })
await page.fill('#email', 'admin@example.com')
await page.fill('#password', 'Password123!')
await page.click('button[type=submit]')
await page.waitForURL(u => !u.toString().includes('sign-in'), { waitUntil: 'domcontentloaded' })

const open = async () => {
  await page.goto(`http://localhost:3000/agents/instructions/${id}`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Advanced', { exact: true }).first().waitFor({ timeout: 60000 })
  await page.waitForTimeout(2500)
  const channels = page.locator('span', { hasText: /^Channels$/ }).first()
  if (!(await channels.isVisible())) await page.getByText('Advanced', { exact: true }).first().click()
  await channels.waitFor()
  return channels.locator('xpath=following-sibling::*[1]//button').first()
}

let picker = await open()
await picker.click()
await page.waitForTimeout(600)
await page.screenshot({ path: `${out}/${mode}-1-channel-picker.png` })

if (mode === 'after') {
  for (const name of [app1, app2]) {
    await page.locator('button', { hasText: name }).last().click()
    await page.waitForTimeout(400)
  }
  await page.screenshot({ path: `${out}/after-2-two-apps-selected.png` })
  await page.keyboard.press('Escape')
  await page.waitForTimeout(3000) // autosave on meta change
  picker = await open()
  await page.screenshot({ path: `${out}/after-3-saved-after-reload.png` })
  await picker.click()
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${out}/after-4-reopened.png` })
}
await browser.close()
console.log('done', mode)
