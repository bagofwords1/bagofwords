// Finish the Data Agent wizard: activate the seeded log tables, save, complete context step.
// NOTE: @playwright/test resolves from the script's own directory (ESM), so run
// these from a copy inside frontend/ (e.g. frontend/.agent-tmp/) against a stack
// booted by tools/agent/boot_stack.sh. Credentials come from env vars only.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.OUT || '../media/es-cloud';
const BASE = 'http://localhost:3000';
mkdirSync(OUT, { recursive: true });

const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const ctx = await b.newContext({ viewport: { width: 1512, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };

await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'commit' });
await page.getByPlaceholder(/email/i).fill('admin@example.com');
await page.getByPlaceholder(/password/i).fill('Password123!');
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2000);

const es = { id: process.env.DS_ID };
if (!es.id) { console.error('DS_ID env required'); process.exit(1); }

await page.goto(`${BASE}/agents/new/${es.id}/schema`, { waitUntil: 'commit' });
await page.waitForTimeout(4000);
await shot('10-schema-before-select');

// check the checkboxes on rows for logs-* tables
const wanted = ['logs-frontend-default', 'logs-backend-default', 'logs-payments-default',
  'logs-checkout-default', 'logs-auth-default', 'logs-search-default'];
for (const name of wanted) {
  const row = page.locator('div,li,tr').filter({ hasText: new RegExp(`^\\s*${name}`) }).last();
  const cb = page.locator(`xpath=//*[normalize-space(text())="${name}"]/ancestor::*[self::tr or contains(@class,"flex")][1]//input[@type="checkbox"]`).first();
  try {
    await cb.check({ timeout: 3000 });
  } catch {
    // fallback: click the checkbox nearest the text
    const label = page.getByText(name, { exact: true }).first();
    const handle = await label.elementHandle();
    await page.evaluate((el) => {
      let n = el;
      for (let i = 0; i < 6 && n; i++) {
        const cb2 = n.querySelector('input[type=checkbox]');
        if (cb2) { cb2.click(); return; }
        n = n.parentElement;
      }
    }, handle);
  }
  await page.waitForTimeout(300);
}
await page.waitForTimeout(800);
await shot('11-schema-selected');
const active = await page.locator('body').innerText();
console.log('active line:', (active.match(/\d+\/\d+ active/) || ['?'])[0]);

await page.getByRole('button', { name: /save & continue/i }).click();
await page.waitForTimeout(5000);
await shot('12-context-step');
console.log('URL:', page.url());

// context step: click through the final save/finish button if present
for (const re of [/save & continue/i, /finish/i, /done/i, /continue/i, /save/i]) {
  const btn = page.getByRole('button', { name: re }).first();
  if (await btn.isVisible().catch(() => false)) {
    await btn.click().catch(() => {});
    await page.waitForTimeout(4000);
    console.log('clicked', re);
    break;
  }
}
await shot('13-wizard-done');
console.log('URL:', page.url());
const t = await page.locator('body').innerText();
console.log('SNIPPET:', t.slice(0, 900).replace(/\n{2,}/g, '\n'));
await ctx.close(); await b.close(); console.log('SELECT_TABLES_DONE');
