// Sign in once and save the session for the other drivers in this folder.
//   cd frontend && OUT=/tmp/connectionless-agent node ../tools/agent/connectionless_agent/login.cjs
// Env: BOW_EMAIL, BOW_PASSWORD (defaults: the seed_org.py admin), BOW_ORIGIN.
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const OUT = process.env.OUT || '/tmp/connectionless-agent';
  const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
  fs.mkdirSync(OUT + '/shots', { recursive: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(origin + '/users/sign-in', { waitUntil: 'load' });
  await page.fill('#email', process.env.BOW_EMAIL || 'admin@example.com');
  await page.fill('#password', process.env.BOW_PASSWORD || 'Password123!');
  await Promise.all([page.waitForNavigation({ waitUntil: 'load' }).catch(() => {}), page.click('button[type=submit]')]);
  await page.waitForTimeout(2000);
  if (page.url().includes('/onboarding')) {
    try { await page.getByText('Skip', { exact: false }).first().click({ timeout: 4000 }); await page.waitForTimeout(1500); } catch {}
  }
  await ctx.storageState({ path: OUT + '/state.json' });
  console.log('signed in; session saved to', OUT + '/state.json');
  await browser.close();
})();
