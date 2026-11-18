// node 02_chat.js <tag> "<prompt>"  -> prints the report url
const { open } = require('./lib');
(async () => {
  const tag = process.argv[2] || '02';
  const prompt = process.argv[3] || 'Show orders by region';
  const { browser, page, shot, BASE } = await open(tag);
  await page.goto(BASE + '/');
  const input = page.locator('[contenteditable="true"]').first();
  await input.waitFor({ timeout: 120000 });
  await input.click();
  await page.keyboard.type(prompt);
  await page.waitForTimeout(800);
  await shot('typed');
  await page.locator('button[class*="rounded-full"]').last().click();
  await page.waitForURL(/\/reports\//, { timeout: 120000 });
  console.log('report', page.url());
  // done when the stop button is gone
  await page.waitForTimeout(4000);
  for (let i = 0; i < 90; i++) {
    const busy = await page.locator('[data-testid="stop-button"]').count();
    if (!busy) break;
    await page.waitForTimeout(2000);
  }
  await page.waitForTimeout(3000);
  await shot('done');
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
