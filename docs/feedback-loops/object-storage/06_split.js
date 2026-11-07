// Attach on pod A, wait for the harness to swap in a fresh pod B, then send.
const { launch, shot, BASE, DIR } = require('./common');
const fs = require('fs');
const [question, file] = process.argv.slice(2);
(async () => {
  const { browser, page } = await launch();
  await page.goto(BASE, { timeout: 120000 });
  await page.waitForSelector('[contenteditable="true"]', { timeout: 120000 });
  await page.waitForTimeout(2000);
  await page.locator('button:has([class*="paper-clip"])').first().click();
  const input = page.locator('input[type="file"]').first();
  await input.waitFor({ state: 'attached', timeout: 30000 });
  const up = page.waitForResponse(r => r.url().endsWith('/api/files') && r.request().method() === 'POST', { timeout: 60000 });
  await input.setInputFiles(file);
  console.log('uploaded on pod A:', (await up).status());
  await page.waitForTimeout(3000);
  await page.mouse.click(60, 60);
  const box = page.locator('[contenteditable="true"]').first();
  await box.click(); await box.type(question, { delay: 5 });
  fs.writeFileSync(`${DIR}/ready`, '1');
  while (!fs.existsSync(`${DIR}/go`)) await page.waitForTimeout(1000);
  await page.locator('button.w-7.h-7.rounded-full').last().click();
  await page.waitForURL(/\/reports\//, { timeout: 120000 });
  const t0 = Date.now(); await page.waitForTimeout(8000);
  while (Date.now() - t0 < 600000) {
    if (!(await page.locator('[data-testid="stop-button"]').count()) && !(await page.getByText('Thinking', { exact: false }).count())) break;
    await page.waitForTimeout(4000);
  }
  await page.waitForTimeout(3000);
  await shot(page, 'split_answer');
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
