import { test } from '@playwright/test';
const URL = 'file://' + __dirname + '/footer.html';
test.use({ viewport: { width: 760, height: 820 } });
test('broken', async ({ page }) => {
  await page.goto(URL);
  await page.evaluate(() => (document.getElementById('viewport')!.scrollTop = 0));
  await page.locator('#viewport').screenshot({ path: '/tmp/footer-height-verify/broken.png' });
});
test('fixed', async ({ page }) => {
  await page.goto(URL + '?fixed=1');
  await page.locator('#viewport').screenshot({ path: '/tmp/footer-height-verify/fixed.png' });
});
