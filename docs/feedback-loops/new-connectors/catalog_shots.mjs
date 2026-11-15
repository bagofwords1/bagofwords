// Sign in, open the add-connection modal, capture catalog views. Usage: node catalog_shots.mjs <prefix>
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');
const prefix = process.argv[2] || 'after';
const OUT = process.env.SHOTS_DIR || '/tmp/bow-connector-shots';
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const page = await browser.newPage({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });
page.setDefaultTimeout(60000);
await page.goto('http://localhost:3000/users/sign-in');
await page.fill('#email', 'admin@example.com');
await page.fill('#password', 'Password123!');
await page.click('button[type=submit]');
await page.waitForURL(u => !u.toString().includes('sign-in'), { timeout: 60000 });
await page.context().storageState({ path: `${OUT}/state.json` });
const skip = page.getByText('Skip onboarding');
if (await skip.count()) { await skip.first().click(); await page.waitForTimeout(2000); }
await page.goto('http://localhost:3000/agents/new');
await page.waitForTimeout(3000);
const skip2 = page.getByText('Skip onboarding');
if (await skip2.count()) { await skip2.first().click(); await page.waitForTimeout(2000); await page.goto('http://localhost:3000/agents/new'); }
const create = page.getByText('Create new connection');
if (!(await page.getByText('Add Connection').count()) && (await create.count())) await create.click();
const modal = page.locator('[id^=headlessui-dialog-panel]').last();
await modal.waitFor();
await page.waitForTimeout(1500);
const search = modal.locator('input').first();
for (const [name, chip, term] of [['files', 'Files & object store', ''], ['search-nosql', null, 'nosql'], ['search-lakehouse', null, 'lakehouse'], ['search-storage', null, 'storage'], ['search-hadoop', null, 'hive'], ['search-aws', null, 'amazon']]) {
  await search.fill(term);
  if (chip) { const b = modal.getByRole('button', { name: new RegExp(`^${chip}`) }); if (await b.count()) await b.first().click(); }
  else { const all = modal.getByRole('button', { name: /^All/ }); if (await all.count()) await all.first().click(); }
  await page.waitForTimeout(700);
  await modal.screenshot({ path: `${OUT}/${prefix}-${name}.png` });
  console.log('shot', `${prefix}-${name}.png`);
}
await browser.close();
