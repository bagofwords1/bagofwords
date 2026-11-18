// node 04_refresh.js <tag> <report_id>  -> clicks the dashboard's Refresh and reports the toast
const { open } = require('./lib');
(async () => {
  const [tag, id] = process.argv.slice(2);
  const { browser, page, shot, BASE } = await open(tag);
  await page.goto(BASE + '/reports/' + id);
  await page.getByText('Created Data').first().waitFor({ timeout: 120000 });
  const tab = page.getByRole('button', { name: 'Dashboard', exact: true }).first();
  if (!(await page.locator('[data-testid="frame-version-picker"]').count())) {
    if (await tab.count()) await tab.click(); else { await page.locator('button:has(span[class*="view-columns"])').first().click(); await page.waitForTimeout(1000); await page.getByRole('button', { name: 'Dashboard', exact: true }).first().click(); }
  }
  await page.locator('[data-testid="frame-version-picker"]').waitFor({ timeout: 60000 });
  await page.waitForTimeout(5000);
  const rerun = page.waitForResponse(r => /\/api\/reports\/.*\/rerun/.test(r.url()), { timeout: 180000 });
  await page.locator('button:has(span[class~="i-heroicons:arrow-path"])').first().click();
  const resp = await rerun;
  const body = await resp.json().catch(() => ({}));
  console.log('rerun', resp.status(), JSON.stringify({ total: body.steps_total, ok: body.steps_succeeded, failed: body.steps_failed, failures: body.failures || body.failed_steps }).slice(0, 600));
  await page.waitForTimeout(2500);
  await shot('toast');
  await page.waitForTimeout(6000);
  await shot('after');
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
