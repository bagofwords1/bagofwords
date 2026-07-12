import { createRequire } from 'module';
const require = createRequire('/home/user/bagofwords/frontend/');
const { chromium } = require('playwright');
import fs from 'fs';
const OUT = '/home/user/bagofwords/.agents/sandbox-feedback-loop/shots';
const BASE = 'http://localhost:3000';
const SEED = JSON.parse(fs.readFileSync('/home/user/bagofwords/.agents/sandbox-feedback-loop/runtime/seed.json'));
const shot = async (p, n) => { await p.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }); console.log('shot:', n); };
const run = async () => {
  const browser = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  page.setDefaultTimeout(25000);
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  await page.locator('#email').fill('admin@example.com');
  await page.locator('#password').fill('Password123!');
  await page.locator('button[type="submit"]').first().click();
  try { await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 }); } catch (e) {}
  await page.waitForTimeout(2000);
  // File-shaped agent → scope view
  await page.goto(`${BASE}/agents/new/${SEED.ds_id}/schema`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  await shot(page, '12-file-scope-step');
  await browser.close();
  console.log('done');
};
run().catch((e) => { console.error('ERR', e); process.exit(1); });
