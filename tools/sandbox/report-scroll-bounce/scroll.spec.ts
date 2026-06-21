import { test, expect, Page } from '@playwright/test';
const URL = 'file://' + __dirname + '/scroll.html';

async function bounceStats(page: Page) {
  await page.waitForTimeout(1500); // let the 80-frame driver finish
  return page.evaluate(() => {
    const s = (window as any).__scrollTops as number[];
    let flips = 0;
    for (let i = 2; i < s.length; i++) {
      const a = s[i] - s[i - 1], b = s[i - 1] - s[i - 2];
      if (a !== 0 && b !== 0 && Math.sign(a) !== Math.sign(b)) flips++; // direction reversals = bounce
    }
    const min = Math.min(...s), max = Math.max(...s);
    return { flips, amplitude: max - min };
  });
}

test('BROKEN (current): viewport bounces as the child height jitters', async ({ page }) => {
  await page.goto(URL);
  const { flips, amplitude } = await bounceStats(page);
  expect(flips).toBeGreaterThan(10);   // scrollTop reverses direction repeatedly
  expect(amplitude).toBeGreaterThanOrEqual(3); // visibly jumps by ~the jitter
});

test('FIXED (amplifier hardening): viewport stays put despite the jitter', async ({ page }) => {
  await page.goto(URL + '?fixed=1');
  const { flips, amplitude } = await bounceStats(page);
  expect(flips).toBe(0);          // no direction reversals
  expect(amplitude).toBeLessThanOrEqual(2);
});
