const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const root = path.resolve(__dirname, '../..');
const { chromium } = require(path.join(root, 'frontend/node_modules/playwright'));
const run = process.env.BOW_DATA_APP_RUN || '/tmp/bow-data-app-run';
let browser;
(async () => {
  const apps = JSON.parse(fs.readFileSync(run + '/apps/manifest.json'));
  browser = await chromium.launch();
  const context = await browser.newContext({ storageState: run + '/storage.json', viewport: { width: 1440, height: 1100 } });
  const page = await context.newPage();
  const results = [];
  for (const slug of ['commerce', 'catalog', 'reps']) {
    await page.goto('http://localhost:3000/r/' + apps[slug].report_id);
    await page.locator('iframe').first().waitFor();
    const frame = await (await page.locator('iframe').first().elementHandle()).contentFrame();
    await frame.locator('#root h1').waitFor();
    await frame.locator('[data-bow-ibtn]').first().click();
    const panel = frame.locator('[data-bow-panel]');
    await panel.waitFor();
    assert.ok((await panel.innerText()).length > 30);
    await frame.locator('body').press('Escape');
    await panel.waitFor({ state: 'detached' });
    results.push(slug + ': source details open and dismiss with Escape');
    if (slug === 'reps') {
      const downloadEvent = page.waitForEvent('download');
      await frame.getByRole('button', { name: 'CSV', exact: true }).click();
      const download = await downloadEvent;
      const csv = fs.readFileSync(await download.path(), 'utf8');
      assert.match(csv, /revenue|Revenue/);
      assert.ok(csv.trim().split('\n').length > 1);
      results.push('Representative account CSV contains actual data rows');
    }
  }
  await page.goto('http://localhost:3000/reports/' + apps.commerce.report_id);
  await page.locator('iframe').first().waitFor({ timeout: 30000 });
  const frame = await (await page.locator('iframe').first().elementHandle()).contentFrame();
  await frame.getByRole('heading', { name: 'Commerce Workbench', exact: true }).waitFor();
  await page.screenshot({ path: path.join(root, 'media/pr/data-app-generation/commerce-editor.png') });
  results.push('Saved app opens in the report editor as well as the shared viewer');
  fs.writeFileSync(run + '/inspection-results.json', JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
  await browser.close();
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => { if (browser) await browser.close(); });
