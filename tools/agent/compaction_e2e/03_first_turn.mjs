import { open, shot, BASE, DIR } from './lib.mjs'
import { typeInComposer, send, waitIdle } from './chat.mjs'
import fs from 'node:fs'
const { browser, page } = await open()
await page.goto(BASE + '/')
await page.waitForSelector('[contenteditable="true"]', { timeout: 120000 })
await page.click('button:has([class*="paper-clip"])')
await page.waitForTimeout(1500)
await page.locator('input[type="file"]').first().setInputFiles((process.env.SALES_CSV || DIR + 'sales_2025.csv'))
await page.waitForTimeout(6000)
await shot(page, '03-upload')
await page.mouse.click(60, 60)
await page.waitForTimeout(1000)
await typeInComposer(page, 'Using the attached sales_2025.csv, what is total revenue by region? Show a table.')
await shot(page, '03-typed')
await send(page)
await page.waitForURL(/\/reports\//, { timeout: 120000 })
const rid = page.url().split('/reports/')[1].split(/[?#/]/)[0]
fs.writeFileSync(DIR + 'report_id', rid)
console.log('report', rid)
const ms = await waitIdle(page)
console.log('run ms', ms)
await shot(page, '03-first-answer')
await browser.close()
