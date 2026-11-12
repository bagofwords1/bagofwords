import { open, shot, BASE, DIR } from './lib.mjs'
import { typeInComposer, send } from './chat.mjs'
import fs from 'node:fs'
const rid = fs.readFileSync(DIR + 'report_id', 'utf8').trim()
const PROMPTS = JSON.parse(fs.readFileSync(new URL(process.env.PROMPTS || 'prompts.json', import.meta.url), 'utf8'))
const start = parseInt(process.env.START || '0')
const { browser, page } = await open()
await page.goto(BASE + '/reports/' + rid)
await page.waitForSelector('[contenteditable="true"]', { timeout: 120000 })
await page.waitForTimeout(3000)
let sawCompacting = 0
for (let i = start; i < PROMPTS.length; i++) {
  await typeInComposer(page, PROMPTS[i])
  await send(page)
  const t0 = Date.now()
  await page.waitForTimeout(2500)
  while (Date.now() - t0 < 600000) {
    if (await page.locator('[data-testid="compacting-indicator"]').count()) {
      sawCompacting++
      await shot(page, `${process.env.TAG || "05"}-compacting-live-${i}-${sawCompacting}`)
    }
    const running = await page.locator('[data-testid="stop-button"]').count()
    if (!running) break
    await page.waitForTimeout(700)
  }
  await page.waitForTimeout(2500)
  console.log('turn', i, 'done in', Math.round((Date.now() - t0) / 1000), 's divider=', await page.locator('[data-testid="compaction-divider"]').count())
  await shot(page, `${process.env.TAG || "05"}-turn-${i}`)
}
console.log('sawCompacting', sawCompacting)
await browser.close()
