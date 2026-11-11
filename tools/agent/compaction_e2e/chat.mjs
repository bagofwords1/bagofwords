// Shared chat helpers for driving the composer like a user.
import { shot } from './lib.mjs'
export async function typeInComposer(page, text) {
  const ed = page.locator('[contenteditable="true"]').last()
  await ed.click()
  await page.keyboard.type(text, { delay: 5 })
}
export async function send(page) {
  await page.locator('button[class*="rounded-full"]').last().click()
}
export async function waitIdle(page, maxMs = 600000) {
  const t0 = Date.now()
  await page.waitForTimeout(3000)
  while (Date.now() - t0 < maxMs) {
    const stop = await page.locator('[data-testid="stop-button"]').count()
    if (!stop) { await page.waitForTimeout(2000); if (!(await page.locator('[data-testid="stop-button"]').count())) return Date.now() - t0 }
    await page.waitForTimeout(2000)
  }
  throw new Error('timeout waiting for run')
}
