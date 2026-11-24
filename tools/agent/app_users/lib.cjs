// Shared by the app-user loop (docs/feedback-loops/2026-11-24-app-users-and-launcher.md).
// Run the scripts in order with one output directory; they hand each other
// signed-in browser states through <out>/../*-state.json.
let chromium
try { ({ chromium } = require('playwright')) } catch { ({ chromium } = require('../../../frontend/node_modules/@playwright/test')) }
const fs = require('fs')
const BOW = process.env.BOW_URL || 'http://localhost:3000'
async function open(opts = {}) {
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, ...opts })
  return { browser, context }
}
async function signIn(context, email, password = process.env.BOW_PASSWORD || 'Password123!') {
  const page = await context.newPage()
  page.setDefaultTimeout(120000); page.setDefaultNavigationTimeout(180000)
  const api = []
  page.on('response', r => { if (r.url().includes('/api/')) api.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`) })
  page.on('console', m => { if (m.type() === 'error' || /intlify/.test(m.text())) api.push('CONSOLE ' + m.text().slice(0, 200)) })
  await page.goto(`${BOW}/users/sign-in`)
  await page.fill('#email', email); await page.fill('#password', password)
  await page.click('button[type=submit]')
  await page.waitForURL(u => !u.pathname.startsWith('/users/sign-in'), { timeout: 120000 })
  return { page, api }
}
const results = []
function check(name, ok, detail = '') { results.push({ name, ok: !!ok, detail: String(detail).slice(0, 300) }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${String(detail).slice(0, 200)}`) }
function done(file) { fs.writeFileSync(file, JSON.stringify(results, null, 1)); const bad = results.filter(r => !r.ok).length; console.log(`${results.length - bad}/${results.length} passed`); process.exitCode = bad ? 1 : 0 }
module.exports = { open, signIn, check, done, BOW }
