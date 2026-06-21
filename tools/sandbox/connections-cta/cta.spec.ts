import { test, expect } from '@playwright/test';
const URL = 'file://' + __dirname + '/footer.html';
test.use({ viewport: { width: 320, height: 700 } });

test('EMPTY (no agents/connections): labeled "Add connection" CTA is visible', async ({ page }) => {
  await page.goto(URL + '?empty=1');
  await expect(page.locator('#cta')).toBeVisible();
  await expect(page.locator('#cta')).toHaveText(/Add connection/);
  await expect(page.locator('#plusIcon')).toBeHidden(); // no bare icon in empty state
});

test('POPULATED: compact "+" shown, labeled CTA hidden', async ({ page }) => {
  await page.goto(URL);
  await expect(page.locator('#plusIcon')).toBeVisible();
  await expect(page.locator('#cta')).toBeHidden();
  await expect(page.locator('#viewall')).toBeVisible();
});
