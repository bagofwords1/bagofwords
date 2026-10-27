import { test, expect } from '../fixtures/feature-test'

/*
 * The conversation measure and the reader's text-size preference.
 *
 * These assert the two properties that regressed before: that the composer
 * and the text above it are the same width (they had drifted to ~53vw on the
 * landing page against a fixed column in the report, so the box resized when
 * you pressed enter), and that chat text renders at the `base` step rather
 * than inheriting the 14px row size.
 *
 * The landing composer is the subject because it needs no report, no model
 * and no completion — the tokens under test are global, so a page that
 * renders a composer is enough to prove them.
 */

const FIELD = '[contenteditable="true"].mention-input-field'
const SURFACE = '[data-testid="composer-surface"]'

// 48 x the 14px chat font. The measure is a multiple of the reader's text
// size, not a fixed width, so this is the default-scale value.
const CHAT_MEASURE_PX = 672

/* Both preferences are read once at plugin init, so they have to be in
   storage before the app boots. */
async function withPreferences(
  page: import('@playwright/test').Page,
  prefs: { width?: 'standard' | 'wide'; textScale?: number },
) {
  await page.addInitScript((p) => {
    if (p.width) localStorage.setItem('bow.chat.width', p.width)
    if (p.textScale) localStorage.setItem('bow.chat.textScale', String(p.textScale))
  }, prefs)
}

async function fieldType(page: import('@playwright/test').Page) {
  return page.locator(FIELD).first().evaluate((el) => {
    const cs = getComputedStyle(el)
    return { fontSize: cs.fontSize, lineHeight: cs.lineHeight }
  })
}

test.use({ viewport: { width: 1440, height: 900 } })

test('composer renders at the chat measure and the chat type step', async ({ page }) => {
  await page.goto('/', { waitUntil: 'commit' })
  await expect(page.locator(SURFACE).first()).toBeVisible({ timeout: 15000 })

  const box = await page.locator(SURFACE).first().boundingBox()
  expect(box).not.toBeNull()
  /* The surface sits inside the measure's own padding, so it is at most the
     measure and within one padding step of it — not 53% of the viewport. */
  expect(box!.width).toBeLessThanOrEqual(CHAT_MEASURE_PX)
  expect(box!.width).toBeGreaterThan(CHAT_MEASURE_PX - 80)

  expect(await fieldType(page)).toEqual({ fontSize: '14px', lineHeight: '21px' })
})

test('the reader text-size preference scales chat type and survives a reload', async ({ page }) => {
  await withPreferences(page, { textScale: 1.25 })
  await page.goto('/', { waitUntil: 'commit' })
  await expect(page.locator(SURFACE).first()).toBeVisible({ timeout: 15000 })

  expect(await fieldType(page)).toEqual({ fontSize: '20px', lineHeight: '30px' })

  /* The column follows the text, so characters-per-line stays put instead of
     the line getting cramped at the largest step. */
  const measure = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue('--bow-measure-chat').trim())
  expect(measure).toBe('960px') // 48 x 20px

  await page.reload({ waitUntil: 'commit' })
  await expect(page.locator(SURFACE).first()).toBeVisible({ timeout: 15000 })
  expect(await fieldType(page)).toEqual({ fontSize: '20px', lineHeight: '30px' })
})

test('wide mode lifts the measure cap', async ({ page }) => {
  await withPreferences(page, { width: 'wide' })
  await page.goto('/', { waitUntil: 'commit' })
  await expect(page.locator(SURFACE).first()).toBeVisible({ timeout: 15000 })

  await expect(page.locator('html')).toHaveAttribute('data-chat-width', 'wide')

  const box = await page.locator(SURFACE).first().boundingBox()
  expect(box!.width).toBeGreaterThan(CHAT_MEASURE_PX)
})
