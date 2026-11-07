// Open a finished report and capture each tracked step expanded (evidence of the live tables).
import { chromium } from '@playwright/test';
const [URL, OUT] = process.argv.slice(2);
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const ctx = await browser.newContext({ viewport: { width: 1512, height: 1100 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
await page.goto('http://localhost:3000/users/sign-in', { waitUntil: 'commit' });
await page.locator('#email').waitFor();
await page.locator('#email').fill('admin@example.com');
await page.locator('#password').fill('Password123!');
await Promise.all([page.waitForURL((u) => !u.pathname.includes('sign-in')).catch(() => {}), page.locator('button[type=submit]').click()]);
await page.waitForTimeout(2000);
await page.goto(URL, { waitUntil: 'commit' });
await page.waitForTimeout(5000);
const rows = page.getByText(/^Created Data$/);
const n = await rows.count();
console.log('steps', n);
for (let i = 0; i < n; i++) {
  const row = rows.nth(i);
  await row.scrollIntoViewIfNeeded();
  await row.click().catch(() => {});
  await page.waitForTimeout(2500);
  await row.evaluate((el) => el.scrollIntoView({ block: 'start' }));
  await page.mouse.wheel(0, -60);
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}/14-step-${i + 1}.png` });
  console.log('shot step', i + 1);
}
await browser.close();
