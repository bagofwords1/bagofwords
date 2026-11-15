const { launch, shot, BASE, DIR } = require('./common');
(async () => {
  const { browser, context, page } = await launch({ state: false });
  await page.goto(BASE + '/users/sign-up', { waitUntil: 'commit', timeout: 180000 });
  await page.waitForSelector('#email', { timeout: 300000 });
  await page.fill('#name', 'Dana Analyst');
  await page.fill('#email', 'dana@example.com');
  await page.fill('#password', 'Sup3r-Secret-Pass!');
  await page.click('button[type=submit]');
  await page.waitForURL(/onboarding|reports|\/$/, { timeout: 120000 });
  await page.waitForTimeout(3000);
  await shot(page, '01_after_signup');
  const skip = page.getByText('Skip onboarding');
  if (await skip.count()) { await skip.first().click(); await page.waitForTimeout(2000); }
  await context.storageState({ path: DIR + '/state.json' });
  console.log('url', page.url());
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
