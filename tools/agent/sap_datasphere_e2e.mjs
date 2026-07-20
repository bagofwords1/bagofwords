// Real end-to-end UI verification for the sap_datasphere connector, driven
// through the actual AddConnectionModal against the local mock Datasphere
// server (tools/datasphere/mock_server.py on :8899). Screenshots into OUT.
//
// Run from a copy inside frontend/ so @playwright/test resolves:
//   cp tools/agent/sap_datasphere_e2e.mjs frontend/.agent-tmp/
//   (cd frontend && node .agent-tmp/sap_datasphere_e2e.mjs)
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.OUT || '/home/user/bagofwords/media/sap-datasphere';
const BASE = 'http://localhost:3000';
const CONN_NAME = 'SAP Datasphere (mock)';
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
try { await page.getByText(/skip onboarding/i).click({ timeout: 5000 }); await page.waitForTimeout(1500); } catch {}

// ---- open connector catalog ----
await page.goto(`${BASE}/agents/new?mode=new_connection`, { waitUntil: 'commit' });
await page.waitForTimeout(2500);
try {
  await page.getByText(/skip onboarding/i).click({ timeout: 3000 });
  await page.waitForTimeout(1500);
  await page.goto(`${BASE}/agents/new?mode=new_connection`, { waitUntil: 'commit' });
  await page.waitForTimeout(2500);
} catch {}

// ---- search & open SAP Datasphere ----
const search = page.getByPlaceholder(/search/i).first();
await search.fill('datasphere');
await page.waitForTimeout(900);
await shot('01-catalog-datasphere');
await page.getByText(/^SAP Datasphere$/).first().click();
await page.waitForTimeout(1200);
await shot('02-form-empty');

// ---- fill the schema-generated form ----
await page.locator('input[placeholder*="Sales DB"]').fill(CONN_NAME).catch(() => {});
await page.locator('#host').fill('http://127.0.0.1:8899');
await page.locator('#token_url').fill('http://127.0.0.1:8899/oauth/token');
await page.locator('#client_id').fill('demo-technical-user');
await page.locator('#client_secret').fill('demo-secret');
await shot('03-form-filled');

// ---- Test connection ----
await page.getByRole('button', { name: /test/i }).first().click();
await page.waitForTimeout(6000);
await shot('04-test-connection-result');
const bodyText = await page.locator('body').innerText();
const m = bodyText.match(/(Found \d+ exposed asset[^\n]*)|(Connected[^\n]*Datasphere[^\n]*)|(Connected successfully[^\n]*)/i);
console.log('TEST_RESULT:', m ? m[0] : '(no explicit message found)');

// ---- Create the connection (→ schema discovery / indexing) ----
const createBtn = page.getByRole('button', { name: /^(create|connect|save)/i }).last();
await createBtn.click();
console.log('clicked create');
for (let i = 0; i < 36; i++) {
  await page.waitForTimeout(5000);
  const t = await page.locator('body').innerText();
  if (/Schema discovery/i.test(t) && /Connected|Failed|Complete/i.test(t)) break;
}
await shot('05-indexing-result');

// ---- continue: Connect → name agent → schema/tables ----
try { await page.getByRole('button', { name: /^Connect$/ }).click({ timeout: 8000 }); } catch {}
await page.waitForTimeout(2000);
try { await page.locator('input[placeholder*="Sales"]').first().fill('Datasphere Demo'); } catch {}
await page.waitForTimeout(300);
try { await page.getByRole('button', { name: /save & continue/i }).click({ timeout: 8000 }); } catch {}
await page.waitForURL(/agents\/new\/.*\/schema/, { timeout: 30000 }).catch(() => {});
await page.waitForTimeout(4000);
await shot('06-schema-tables');
console.log('PAGE_URL:', page.url());
const t2 = await page.locator('body').innerText();
const hasModels = /SalesAnalyticModel/.test(t2);
console.log('MODELS_VISIBLE:', hasModels, '| ExpensesModel:', /ExpensesModel/.test(t2), '| SalariesModel:', /SalariesModel/.test(t2));

await ctx.close(); await b.close();
console.log('CONNECT_FLOW_DONE');
