import { test } from '@playwright/test';
const URL = 'file://' + __dirname + '/footer.html';
test.use({ viewport: { width: 300, height: 360 } });
test('empty shot', async ({ page }) => {
  await page.goto(URL + '?empty=1');
  await page.locator('.footer').screenshot({ path: '/tmp/cta-verify/empty-cta.png' });
});
test('populated shot', async ({ page }) => {
  await page.goto(URL);
  await page.locator('.footer').screenshot({ path: '/tmp/cta-verify/populated.png' });
});
