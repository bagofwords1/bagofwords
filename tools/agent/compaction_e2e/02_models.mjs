import { open, shot, BASE } from './lib.mjs'
import fs from 'node:fs'
const KEY = process.env.OPENAI_API_KEY
if (!KEY) throw new Error('set OPENAI_API_KEY')
const WANT = 'gpt-5.6-luna'
const CONTEXT = process.env.CTX || '40000'
const { browser, page } = await open()
await page.goto(BASE + '/settings/models')
await page.getByRole('button', { name: /Add Provider/i }).first().click({ timeout: 120000 })
await page.waitForTimeout(1500)
await page.click('img[alt="openai logo"]')
await page.waitForTimeout(1500)
await page.fill('input[placeholder*="Provider Name" i]', 'OpenAI')
// Api Key: the input right after the "Api Key" label
const keyInput = page.locator('label:has-text("Api Key") + * input, label:has-text("Api Key") ~ input').first()
if (await keyInput.count()) await keyInput.fill(KEY)
else {
  const inp = page.locator('div[role=dialog] input:not([placeholder]):not([type=checkbox])').first()
  await inp.fill(KEY)
}
// Models: keep only WANT, set its context window
const rows = await page.evaluate((want) => {
  const out = []
  const boxes = Array.from(document.querySelectorAll('div[role=dialog] input[type=checkbox]'))
  for (const cb of boxes) {
    let el = cb, txt = ''
    for (let i = 0; i < 8 && el; i++) { el = el.parentElement; if (el && el.innerText.includes('Model ID:')) { txt = el.innerText; break } }
    const m = /Model ID:\s*(\S+)/.exec(txt)
    out.push(m ? m[1] : null)
  }
  return out
}, WANT)
console.log('models', rows.filter(Boolean).join(','))
const boxes = page.locator('div[role=dialog] input[type=checkbox]')
for (let i = 0; i < rows.length; i++) {
  if (!rows[i]) continue
  const want = rows[i] === WANT
  const cb = boxes.nth(i)
  if ((await cb.isChecked()) !== want) await cb.click({ force: true })
}
// context input in WANT's row
const row = page.locator(`div[role=dialog] :text("Model ID: ${WANT}")`).locator('xpath=ancestor::div[.//input[@type="number"]][1]')
const ctxInput = row.locator('input[type=number]').first()
await ctxInput.fill(CONTEXT)
await row.scrollIntoViewIfNeeded()
await shot(page, '02-luna-context')
const test = page.getByRole('button', { name: /Test Connection/i })
if (await test.count()) { await test.first().click(); await page.waitForTimeout(8000); await shot(page, '02-test-connection') }
await page.getByRole('button', { name: /Save Provider/i }).click()
await page.waitForTimeout(5000)
await shot(page, '02-saved')
await browser.close()
