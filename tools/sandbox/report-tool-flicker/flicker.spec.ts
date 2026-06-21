import { test, expect, Page } from '@playwright/test';
const URL = 'file://' + __dirname + '/flicker.html';

// Count how many times the card's view flips between RENDERED <-> DIFF.
async function countFlips(page: Page): Promise<{ flips: number; final: string; views: string[] }> {
  await page.waitForTimeout(2000); // let the id churn (1.5s) finish + settle
  return page.evaluate(() => {
    const v = (window as any).__views as string[];
    let flips = 0;
    for (let i = 1; i < v.length; i++) if (v[i] !== v[i - 1]) flips++;
    return { flips, final: v[v.length - 1], views: v };
  });
}

test('BROKEN (volatile key block.id:exec.id): card remounts and flickers RENDERED<->DIFF', async ({ page }) => {
  await page.goto(URL);
  const { flips, final } = await countFlips(page);
  expect(flips).toBeGreaterThan(3);   // oscillates many times during the churn
  expect(final).toBe('DIFF(v1→v2)');  // only settles once churn stops
});

test('FIXED (stable key block.id): card persists, no flicker', async ({ page }) => {
  await page.goto(URL + '?stable=1');
  const { flips, final } = await countFlips(page);
  expect(flips).toBeLessThanOrEqual(1); // RENDERED -> DIFF once, then stable
  expect(final).toBe('DIFF(v1→v2)');
});
