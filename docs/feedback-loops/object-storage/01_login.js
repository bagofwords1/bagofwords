const { launch, shot, BASE, DIR } = require('./common');
(async () => {
  const { browser, context, page } = await launch({ state: false });
  await page.goto(`${BASE}/users/sign-in`, { timeout: 120000 });
  await page.waitForSelector('input[type="email"], #email', { timeout: 120000 });
  await page.fill('input[type="email"], #email', 'admin@example.com');
  await page.fill('input[type="password"], #password', 'Password123!');
  await page.click('button[type="submit"]');
  await page.waitForURL(u => !u.toString().includes('sign-in'), { timeout: 120000 });
  await page.waitForTimeout(4000);
  if (page.url().includes('onboarding')) {
    const skip = page.getByText('Skip onboarding');
    if (await skip.count()) { await skip.first().click(); await page.waitForTimeout(3000); }
  }
  await shot(page, 'after_login');
  await context.storageState({ path: `${DIR}/state.json` });
  console.log('url', page.url());
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
