const { open } = require('./lib');
(async () => {
  const { browser, ctx, page, shot, BASE } = await open('01', { state: false });
  await page.goto(BASE + '/users/sign-in');
  await page.waitForSelector('#email', { timeout: 120000 });
  await page.fill('#email', 'admin@example.com');
  await page.fill('#password', 'Password123!');
  await page.click('button[type=submit]');
  await page.waitForURL(u => !u.pathname.includes('sign-in'), { timeout: 120000 });
  await page.waitForTimeout(5000);
  await shot('home');
  console.log('url', page.url());
  await ctx.storageState({ path: 'state.json' });
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
