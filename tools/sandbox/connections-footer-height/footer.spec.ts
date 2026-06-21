import { test, expect, Page } from '@playwright/test';
const URL = 'file://' + __dirname + '/footer.html';
test.use({ viewport: { width: 1280, height: 900 } });

// Footer fully inside the simulated viewport box (no scroll of that box needed).
async function footerInViewport(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const vp = document.getElementById('viewport')!.getBoundingClientRect();
    const f = document.getElementById('footer')!.getBoundingClientRect();
    return f.bottom <= vp.bottom + 0.5 && f.top >= vp.top - 0.5;
  });
}
async function viewportScrolls(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const vp = document.getElementById('viewport')!;
    return vp.scrollHeight > vp.clientHeight + 1;
  });
}

test('BROKEN (full height): footer pushed below the viewport by the banner offset', async ({ page }) => {
  await page.goto(URL);
  expect(await footerInViewport(page)).toBe(false); // hidden until you scroll
  expect(await viewportScrolls(page)).toBe(true);    // overflows by ~banner height
});

test('FIXED (banner-aware height): footer visible, no scroll', async ({ page }) => {
  await page.goto(URL + '?fixed=1');
  expect(await footerInViewport(page)).toBe(true);
  expect(await viewportScrolls(page)).toBe(false);
});
