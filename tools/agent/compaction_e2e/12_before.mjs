import { open, shot, BASE, DIR } from './lib.mjs'
import fs from 'node:fs'
const rid = fs.readFileSync(DIR + 'report_id', 'utf8').trim()
const TAG = process.env.TAG
const { browser, page } = await open({ scale: 2 })
await page.goto(BASE + '/reports/' + rid)
await page.waitForSelector('[contenteditable="true"]', { timeout: 180000 })
await page.waitForTimeout(6000)
const div = page.locator('[data-testid="compaction-divider"]')
await div.first().scrollIntoViewIfNeeded(); await page.waitForTimeout(800)
await page.screenshot({ path: DIR + `shots/${TAG}-divider.png`, clip: await (async () => { const b = await div.first().boundingBox(); return { x: 300, y: Math.max(b.y - 160, 0), width: 1100, height: 380 } })() })
const ed = page.locator('[contenteditable="true"]').last()
await ed.click(); await page.keyboard.type('/co')
await page.waitForTimeout(1200)
const comp = await page.locator('[data-testid="composer-surface"]').boundingBox()
await page.screenshot({ path: DIR + `shots/${TAG}-composer-slash.png`, clip: { x: comp.x - 20, y: comp.y - 200, width: comp.width + 40, height: comp.height + 220 } })
console.log('slash menu present:', await page.locator('[data-testid="slash-menu"]').count())
await browser.close()
