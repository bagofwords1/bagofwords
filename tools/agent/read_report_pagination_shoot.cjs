// Screenshot read_report cards on a report (see docs/feedback-loops/2026-11-06-read-report-pagination.md).
// usage: node read_report_pagination_shoot.cjs <outdir> <report_id> <tag> [locale]
const { chromium } = require('playwright');
const [SP, RID, TAG, LOCALE] = process.argv.slice(2);
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errs = []; page.on('console', m => { if (m.type() === 'error') errs.push(m.text()) });
  await page.goto('http://localhost:3000/users/sign-in');
  await page.fill('#email', 'admin@example.com'); await page.fill('#password', 'Password123!');
  await page.click('button[type=submit]'); await page.waitForTimeout(4000);
  if (LOCALE) await page.evaluate(l => localStorage.setItem('bow.locale', l), LOCALE);
  await page.goto(`http://localhost:3000/reports/${RID}`);
  await page.getByText('Customer churn deep dive').first().waitFor({ timeout: 120000 });
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${SP}/${TAG}_collapsed.png` });
  const heads = page.locator('div.cursor-pointer:has-text("Customer churn deep dive")');
  console.log('cards', await heads.count());
  if (await heads.count() < 3) { await b.close(); return; }
  await heads.nth(2).click(); await page.waitForTimeout(500);
  await page.getByText('Parameters').first().click(); await page.waitForTimeout(500);
  await page.screenshot({ path: `${SP}/${TAG}_expanded.png`, fullPage: true });
  console.log('errors', JSON.stringify(errs.slice(0, 5)));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
