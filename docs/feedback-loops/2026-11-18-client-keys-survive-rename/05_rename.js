// node 05_rename.js <tag> "<current name>" "<new name>"  -> renames an agent in Agents > settings
const { open } = require('./lib');
(async () => {
  const [tag, from, to] = process.argv.slice(2);
  const { browser, page, shot, BASE } = await open(tag);
  await page.goto(BASE + '/agents');
  const row = page.getByText(from, { exact: true }).first();
  await row.waitFor({ timeout: 120000 });
  await row.hover();
  await page.waitForTimeout(500);
  await shot('agents');
  const nameInput = page.locator('input[placeholder="Name"]');
  if (!(await nameInput.count())) {
    await row.click();
    await page.waitForTimeout(1500);
    await page.locator('button:has(span[class~="i-heroicons:cog-6-tooth"]), a:has(span[class~="i-heroicons:cog-6-tooth"])').filter({ hasText: 'Settings' }).first().click();
  }
  await nameInput.waitFor({ timeout: 30000 });
  await page.waitForTimeout(1500);
  await nameInput.fill(to);
  await shot('typed');
  const put = page.waitForResponse(r => r.request().method() === 'PUT' && /\/api\/data_sources\//.test(r.url()), { timeout: 60000 });
  await page.getByRole('button', { name: 'Save', exact: true }).first().click();
  const resp = await put;
  console.log('PUT', resp.status(), (await resp.json()).name);
  await page.waitForTimeout(2000);
  await shot('saved');
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
