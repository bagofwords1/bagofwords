import { open, shot, BASE, DIR } from './lib.mjs'
import fs from 'node:fs'
const rid = fs.readFileSync(DIR + 'report_id', 'utf8').trim()
const { browser, page } = await open({ scale: 2 })
await page.goto(BASE + '/reports/' + rid)
await page.waitForSelector('[contenteditable="true"]', { timeout: 120000 })
await page.waitForTimeout(5000)
const msgs = page.locator('[data-message-role="system"]')
const last = msgs.last()
await last.scrollIntoViewIfNeeded()
const txt = await last.innerText()
console.log(txt.slice(-700))
const box = page.getByText(/first question/i).last()
if (await box.count()) { await box.scrollIntoViewIfNeeded(); await page.waitForTimeout(500) }
await shot(page, '11-recall-answer')
await browser.close()
