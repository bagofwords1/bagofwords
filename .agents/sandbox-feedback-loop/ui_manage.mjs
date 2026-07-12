import { createRequire } from 'module';
const require = createRequire('/home/user/bagofwords/frontend/');
const { chromium } = require('playwright');
import fs from 'fs';
const OUT = '/home/user/bagofwords/.agents/sandbox-feedback-loop/shots';
const BASE = 'http://localhost:3000';
const SEED = JSON.parse(fs.readFileSync('/home/user/bagofwords/.agents/sandbox-feedback-loop/runtime/seed_mixed.json'));
const shot = async (p, n) => { await p.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }); console.log('shot:', n); };
const run = async () => {
  const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  const p = await b.newPage({ viewport: { width: 1440, height: 1300 } });
  const errs = [];
  p.on('pageerror', e => errs.push(String(e)));
  p.setDefaultTimeout(25000);
  await p.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(1500);
  await p.locator('#email').fill('admin@example.com');
  await p.locator('#password').fill('Password123!');
  await p.locator('button[type="submit"]').first().click();
  try { await p.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 }); } catch (e) {}
  await p.waitForTimeout(2000);
  await p.goto(`${BASE}/old_agents/${SEED.ds_id}/tables`, { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(5000);
  await shot(p, '14-mixed-manage-view');
  console.log('pageerrors:', errs.slice(0,3));
  // check the nav no longer has a Tools tab
  const hasToolsTab = await p.getByRole('link', { name: /^Tools$/ }).count().catch(()=>0);
  console.log('Tools tab present in nav:', hasToolsTab);
  await b.close();
  console.log('done');
};
run().catch((e) => { console.error('ERR', e); process.exit(1); });
