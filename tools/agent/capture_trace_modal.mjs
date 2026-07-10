// Capture the conversation (parallel chip) + Agent Trace modal (waterfall).
import { chromium } from '@playwright/test'
import { mkdirSync, existsSync } from 'node:fs'

const args = process.argv.slice(2)
const opt = (name, dflt) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : dflt }
const base = opt('base', 'http://localhost:3000')
const reportId = opt('report', null)
const outDir = opt('out', './trace-capture')
if (!reportId) { console.error('need --report'); process.exit(2) }
mkdirSync(outDir, { recursive: true })

const exe = process.env.PW_CHROMIUM_PATH
  || (existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined)
const browser = await chromium.launch(exe ? { executablePath: exe } : {})
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()

// Dev-server may recompile after source edits — warm up, then retry login.
for (let attempt = 0; attempt < 3; attempt++) {
  await page.goto(`${base}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {})
  await page.waitForTimeout(4000)
  try {
    await page.fill('#email', opt('email', 'admin@example.com'), { timeout: 15000 })
    await page.fill('#password', opt('password', 'Password123!'), { timeout: 15000 })
    await page.click('button[type="submit"]')
  } catch { continue }
  for (let i = 0; i < 30; i++) { if (!page.url().includes('sign-in')) break; await page.waitForTimeout(1000) }
  if (!page.url().includes('sign-in')) break
}
if (page.url().includes('sign-in')) throw new Error('login did not redirect')
console.log('logged in')

await page.goto(`${base}/reports/${reportId}`, { waitUntil: 'networkidle' }).catch(() => {})
await page.waitForTimeout(8000)
const scrollToBottom = () => page.evaluate(() => {
  document.querySelectorAll('div').forEach((d) => {
    if (d.scrollHeight > d.clientHeight + 80) d.scrollTop = d.scrollHeight
  })
})
await scrollToBottom(); await page.waitForTimeout(800)
await page.screenshot({ path: `${outDir}/conversation-chip.png` })
console.log('captured conversation-chip.png')

// Open the Agent Trace modal: the bug-ant debug button on an AI message.
const candidates = [
  'button[title="View Agent Trace"]',
  'button:has(span.iconify)',
]
let opened = false
const byTitle = page.locator('button[title*="Trace"]')
if (await byTitle.count()) { await byTitle.last().click(); opened = true }
if (!opened) {
  // fall back: click every small icon button until a dialog appears
  const btns = page.locator('button.w-6.h-6')
  const n = await btns.count()
  for (let i = n - 1; i >= 0 && !opened; i--) {
    await btns.nth(i).click().catch(() => {})
    await page.waitForTimeout(1200)
    if (await page.locator('text=Timeline').count()) opened = true
  }
}
await page.waitForTimeout(8000) // trace fetch + render
await page.screenshot({ path: `${outDir}/trace-waterfall.png` })
console.log('captured trace-waterfall.png (opened=' + opened + ')')
await browser.close()
