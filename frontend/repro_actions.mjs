import { chromium } from '@playwright/test';
const origin = 'http://localhost:3000';
const execPath = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const browser = await chromium.launch({ executablePath: execPath });
const context = await browser.newContext({ viewport: { width: 1500, height: 950 } });
const page = await context.newPage();
const errors = [];
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text()); });

await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', 'admin@example.com').catch(()=>{});
await page.fill('#password', 'Password123!').catch(()=>{});
await Promise.all([ page.waitForNavigation({ waitUntil: 'networkidle' }).catch(()=>{}), page.click('button[type=submit]').catch(()=>{}) ]);
await page.waitForTimeout(1500);
await page.goto(`${origin}/settings/models`, { waitUntil: 'networkidle' }).catch(()=>{});
await page.waitForTimeout(2500);

// Find the actions (ellipsis) buttons.
const actionBtns = page.locator('button:has(.i-heroicons-ellipsis-vertical), button[aria-haspopup]');
const count = await actionBtns.count();
console.log('action-ish buttons found:', count);
// Try clicking the trailing ellipsis in the last column of first row.
const rowBtns = page.locator('table tbody tr').first().locator('button');
const n = await rowBtns.count();
console.log('buttons in first row:', n);
try {
  await rowBtns.last().click({ timeout: 4000 });
  await page.waitForTimeout(1200);
  console.log('clicked last button in row 1');
} catch (e) { console.log('click failed:', e.message); }
await page.screenshot({ path: '../media/pr/auto-model-routing/_repro-actions.png' });
console.log('ERRORS:', JSON.stringify(errors, null, 2));
await context.close(); await browser.close();
