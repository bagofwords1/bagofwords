const { launch, shot, BASE, DIR } = require('./common');
const fs = require('fs');
const question = process.argv[2];
const file = process.argv[3];
const reportUrl = process.argv[4];
(async () => {
  const { browser, page } = await launch();
  await page.goto(reportUrl || BASE, { timeout: 120000 });
  await page.waitForSelector('[contenteditable="true"]', { timeout: 120000 });
  await page.waitForTimeout(2000);
  if (file) {
    await page.locator('button:has([class*="paper-clip"])').first().click();
    const input = page.locator('input[type="file"]').first();
    await input.waitFor({ state: 'attached', timeout: 30000 });
    await input.setInputFiles(file);
    await page.waitForTimeout(6000);
    await shot(page, 'attached');
    await page.mouse.click(60, 60);
    await page.waitForTimeout(1000);
  }
  const box = page.locator('[contenteditable="true"]').first();
  await box.click();
  await box.type(question, { delay: 5 });
  await page.waitForTimeout(800);
  await page.locator('button.w-7.h-7.rounded-full').last().click();
  if (!reportUrl) await page.waitForURL(/\/reports\//, { timeout: 120000 });
  console.log('report', page.url());
  const t0 = Date.now();
  await page.waitForTimeout(8000);
  while (Date.now() - t0 < 600000) {
    const stop = await page.locator('[data-testid="stop-button"]').count();
    const thinking = await page.getByText('Thinking', { exact: false }).count();
    if (!stop && !thinking) break;
    await page.waitForTimeout(4000);
  }
  await page.waitForTimeout(3000);
  await shot(page, 'answer');
  fs.writeFileSync(`${DIR}/last_report.txt`, page.url());
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
