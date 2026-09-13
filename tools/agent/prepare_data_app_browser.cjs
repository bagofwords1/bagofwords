// Reuse the sandbox API login; never write credentials into the repository.
const fs = require('fs');
const path = require('path');
const { chromium } = require(path.resolve(__dirname, '../../frontend/node_modules/playwright'));
const run = process.env.BOW_DATA_APP_RUN || '/tmp/bow-data-app-run';
(async () => {
  const session = JSON.parse(fs.readFileSync(path.join(run, 'session.json')));
  const browser = await chromium.launch();
  const context = await browser.newContext();
  await context.addCookies([{ name: 'auth.token', value: session.admin.token, url: 'http://localhost:3000', sameSite: 'Lax' }]);
  const page = await context.newPage();
  await page.goto('http://localhost:3000');
  await page.waitForURL(url => !url.pathname.includes('sign-in'));
  await context.storageState({ path: path.join(run, 'storage.json') });
  fs.chmodSync(path.join(run, 'storage.json'), 0o600);
  await browser.close();
  console.log('Authenticated sandbox browser state prepared');
})().catch(error => { console.error(error.message); process.exitCode = 1; });
