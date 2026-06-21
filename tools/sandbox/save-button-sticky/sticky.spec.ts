import { test, expect, Page } from '@playwright/test';

const URL = 'file://' + __dirname + '/harness.html';

// True when the Save button is fully inside the scroll container's visible box.
async function saveButtonVisible(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const scroll = document.getElementById('scroll')!.getBoundingClientRect();
    const btn = document.getElementById('saveBtn')!.getBoundingClientRect();
    return btn.top >= scroll.top - 0.5 && btn.bottom <= scroll.bottom + 0.5;
  });
}

test.use({ viewport: { width: 1280, height: 800 } });

test('panel overflows so the save bar is below the fold (precondition)', async ({ page }) => {
  await page.goto(URL); // no ?sticky -> reproduces the reported bug
  const overflows = await page.evaluate(() => {
    const s = document.getElementById('scroll')!;
    return s.scrollHeight > s.clientHeight + 1;
  });
  expect(overflows, 'scroll container must overflow for this bug to exist').toBe(true);
});

test('BROKEN (non-sticky): save button is NOT visible at scrollTop=0', async ({ page }) => {
  await page.goto(URL);
  await page.evaluate(() => (document.getElementById('scroll')!.scrollTop = 0));
  expect(await saveButtonVisible(page)).toBe(false); // hidden until you scroll
});

test('FIXED (sticky): save button stays visible at every scroll position', async ({ page }) => {
  await page.goto(URL + '?sticky=1');
  const positions = await page.evaluate(() => {
    const s = document.getElementById('scroll')!;
    return [0, s.scrollHeight / 2, s.scrollHeight];
  });
  for (const top of positions) {
    await page.evaluate((t) => (document.getElementById('scroll')!.scrollTop = t), top);
    await page.waitForTimeout(50);
    expect(await saveButtonVisible(page), `visible at scrollTop=${top}`).toBe(true);
  }
});
