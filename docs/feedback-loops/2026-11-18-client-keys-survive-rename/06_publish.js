const { open } = require('./lib');
(async () => {
  const [tag, id] = process.argv.slice(2);
  const { browser, page, shot, BASE } = await open(tag);
  await page.goto(BASE + '/reports/' + id);
  await page.locator('[data-testid="publish-dashboard"]').waitFor({ timeout: 120000 });
  await page.waitForTimeout(3000);
  await page.locator('[data-testid="publish-dashboard"]').click();
  await page.waitForTimeout(3000);
  await shot('modal');
  const pub = page.waitForResponse(r => r.request().method() === 'POST' && /publish/.test(r.url()), { timeout: 120000 });
  await page.locator('[data-testid="publish-popover-confirm"]').click();
  const resp = await pub;
  console.log('publish', resp.status(), resp.url(), JSON.stringify(await resp.json().catch(() => ({}))).slice(0, 500));
  await page.waitForTimeout(5000);
  await shot('published');
  console.log(await page.locator('button').evaluateAll(bs => bs.map(b => (b.getAttribute('data-testid')||'') + '|' + (b.innerText||'').trim().slice(0,30)).filter(s => s !== '|').slice(-25)));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
