// Connect the local HANA Express through the real UI (AddConnectionModal),
// then walk to the schema/tables page. Screenshots into OUT.
// Adapted from tools/agent/es_cloud_connect_elastic.mjs.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.OUT || '../media/sap-hana';
const BASE = 'http://localhost:3000';
const CONN_NAME = 'SAP HANA Express (local)';
mkdirSync(OUT, { recursive: true });

const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const ctx = await b.newContext({ viewport: { width: 1512, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };

// ---- login ----
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'commit' });
await page.getByPlaceholder(/email/i).fill('admin@example.com');
await page.getByPlaceholder(/password/i).fill('Password123!');
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2000);

// ---- skip onboarding if the wizard shows up ----
try {
  await page.getByText(/skip onboarding/i).click({ timeout: 5000 });
  await page.waitForTimeout(1500);
  console.log('skipped onboarding');
} catch {}

// ---- open connector catalog ----
await page.goto(`${BASE}/agents/new?mode=new_connection`, { waitUntil: 'commit' });
await page.waitForTimeout(2500);
try {
  await page.getByText(/skip onboarding/i).click({ timeout: 3000 });
  await page.waitForTimeout(1500);
  await page.goto(`${BASE}/agents/new?mode=new_connection`, { waitUntil: 'commit' });
  await page.waitForTimeout(2500);
} catch {}

// search sap hana and open its form
const search = page.getByPlaceholder(/search/i).first();
await search.fill('sap');
await page.waitForTimeout(800);
await shot('01-catalog-sap');
await page.getByText(/^SAP HANA$/).first().click();
await page.waitForTimeout(1200);
await shot('02-sap-hana-form-empty');

// ---- fill the form ----
await page.locator('input[placeholder*="Sales DB"]').fill(CONN_NAME);
await page.locator('#host').fill('127.0.0.1');
await page.locator('#port').fill('39041');
await page.locator('#schema').fill('BOW_DEMO');
// HANA Express serves a self-signed cert on the tenant port: keep Encrypt ON
// (as with real HANA Cloud / Datasphere) and turn Verify SSL off. UToggle
// renders role=switch buttons in field order: encrypt(0), verify_ssl(1).
await page.getByRole('switch').nth(1).click();
await page.locator('#user').fill('SYSTEM');
await page.locator('#password').fill('HXEHana1');
await shot('03-sap-hana-form-filled');

// ---- Test connection ----
await page.getByRole('button', { name: /test/i }).first().click();
await page.waitForTimeout(6000);
await shot('04-test-connection-result');
const bodyText = await page.locator('body').innerText();
const m = bodyText.match(/Successfully connected[^\n]*/i);
console.log('TEST_RESULT:', m ? m[0] : '(no explicit message found)');

// ---- Create the connection (goes to the "indexing" step) ----
const createBtn = page.getByRole('button', { name: /^(create|connect|save)/i }).last();
await createBtn.click();
console.log('clicked create');
for (let i = 0; i < 36; i++) {
  await page.waitForTimeout(5000);
  const t = await page.locator('body').innerText();
  if (/Schema discovery/i.test(t) && /Connected|Failed/.test(t)) break;
}
await shot('05-indexing-result');
await page.getByRole('button', { name: /^Connect$/ }).click();
await page.waitForTimeout(2000);

// ---- name the agent and continue to the schema/tables page ----
await page.locator('input[placeholder*="Sales"]').first().fill('HANA Demo');
await page.waitForTimeout(300);
await page.getByRole('button', { name: /save & continue/i }).click();
await page.waitForURL(/agents\/new\/.*\/schema/, { timeout: 30000 }).catch(() => {});
await page.waitForTimeout(4000);
await shot('06-schema-tables');
console.log('PAGE_URL:', page.url());
const t2 = await page.locator('body').innerText();
const hasTables = /CUSTOMERS/.test(t2) && /V_REVENUE_BY_COUNTRY/.test(t2);
console.log('TABLES_VISIBLE:', hasTables);

await ctx.close(); await b.close();
console.log('CONNECT_FLOW_DONE');
