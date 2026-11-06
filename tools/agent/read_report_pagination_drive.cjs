// Drive the read_report pagination loop through the UI (see docs/feedback-loops/2026-11-06-read-report-pagination.md).
// usage: node read_report_pagination_drive.cjs <outdir> "<prompt>" [tag]
const { chromium } = require('playwright');
const fs = require('fs');
const SP = process.argv[2], PROMPT = process.argv[3], TAG = process.argv[4] || 'run';
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const log = fs.createWriteStream(`${SP}/${TAG}_http.jsonl`);
  page.on('response', r => { if (r.url().includes('/api/')) log.write(JSON.stringify({ s: r.status(), m: r.request().method(), u: r.url() }) + '\n'); });
  await page.goto('http://localhost:3000/users/sign-in');
  await page.fill('#email', 'admin@example.com'); await page.fill('#password', 'Password123!');
  await page.click('button[type=submit]'); await page.waitForTimeout(4000);
  if (page.url().includes('/onboarding')) { await page.getByText('Skip onboarding').click().catch(()=>{}); await page.waitForTimeout(2000); }
  await page.goto('http://localhost:3000/'); 
  const input = page.locator('[contenteditable="true"]').first();
  await input.waitFor({ timeout: 120000 });
  await page.waitForTimeout(2000);
  await input.click(); await page.keyboard.type(PROMPT);
  await page.screenshot({ path: `${SP}/${TAG}_1_typed.png` });
  await page.locator('button[class*="rounded-full"]').last().click();
  await page.waitForURL(/\/reports\//, { timeout: 60000 });
  const t0 = Date.now();
  while (Date.now() - t0 < 180000) {
    await page.waitForTimeout(3000);
    const busy = await page.locator('[data-testid="stop-button"]').count();
    if (!busy) break;
  }
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${SP}/${TAG}_2_done.png`, fullPage: false });
  console.log(JSON.stringify({ url: page.url() }));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
