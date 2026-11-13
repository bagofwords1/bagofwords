import { chromium } from 'playwright'
import fs from 'node:fs'
// Artifacts (state.json, shots/, http.jsonl, video/) land in BOW_E2E_OUT (default ./out).
export const DIR = (process.env.BOW_E2E_OUT || new URL('./out/', import.meta.url).pathname).replace(/\/?$/, '/')
fs.mkdirSync(DIR + 'shots', { recursive: true })
export const BASE = 'http://localhost:3000'
export async function open({ state = true, scale = 1 } = {}) {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })
  const opts = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: scale }
  if (state && fs.existsSync(DIR + 'state.json')) opts.storageState = DIR + 'state.json'
  const ctx = await browser.newContext(opts)
  const page = await ctx.newPage()
  const log = fs.createWriteStream(DIR + 'http.jsonl', { flags: 'a' })
  page.on('response', async r => {
    const u = r.url()
    if (!u.includes('/api/')) return
    log.write(JSON.stringify({ t: Date.now(), m: r.request().method(), u, s: r.status() }) + '\n')
  })
  page.on('console', m => { if (m.type() === 'error') fs.appendFileSync(DIR + 'console.log', m.text() + '\n') })
  return { browser, ctx, page }
}
export async function shot(page, name, scale) {
  await page.screenshot({ path: DIR + 'shots/' + name + '.png' })
  console.log('shot', name)
}
