import { test } from '@playwright/test';
const URL = 'file://' + __dirname + '/harness.html';
test.use({ viewport: { width: 1000, height: 800 } });

test('shot broken', async ({ page }) => {
  await page.goto(URL);
  await page.evaluate(() => (document.getElementById('scroll')!.scrollTop = 0));
  await page.screenshot({ path: '/tmp/sticky-verify/before-broken.png' });
});
test('shot fixed', async ({ page }) => {
  await page.goto(URL + '?sticky=1');
  await page.evaluate(() => (document.getElementById('scroll')!.scrollTop = 0));
  await page.screenshot({ path: '/tmp/sticky-verify/after-fixed.png' });
});
