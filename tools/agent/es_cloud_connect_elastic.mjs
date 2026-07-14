// Connect the Elastic Cloud instance through the real UI (AddConnectionModal).
// NOTE: @playwright/test resolves from the script's own directory (ESM), so run
// these from a copy inside frontend/ (e.g. frontend/.agent-tmp/) against a stack
// booted by tools/agent/boot_stack.sh. Credentials come from env vars only.
// Env: ES_URL, ES_API_KEY_ENCODED. Screenshots into OUT.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.OUT || '../media/es-cloud';
const BASE = 'http://localhost:3000';
const ES_URL = process.env.ES_URL;
const ES_KEY = process.env.ES_API_KEY_ENCODED;
const CONN_NAME = process.env.CONN_NAME || 'Elastic Cloud (observability)';
if (!ES_URL || !ES_KEY) { console.error('missing ES_URL / ES_API_KEY_ENCODED'); process.exit(2); }
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
await shot('01-logged-in');

// ---- skip onboarding if the wizard shows up ----
try {
  await page.getByText(/skip onboarding/i).click({ timeout: 5000 });
  await page.waitForTimeout(1500);
  console.log('skipped onboarding');
} catch {}

// ---- open Create Data Agent; the connection modal auto-opens when no connections exist ----
await page.goto(`${BASE}/agents/new?mode=new_connection`, { waitUntil: 'commit' });
await page.waitForTimeout(2500);
await shot('02-connector-catalog');
// the wizard may reappear on this route too
try {
  await page.getByText(/skip onboarding/i).click({ timeout: 3000 });
  await page.waitForTimeout(1500);
  await page.goto(`${BASE}/agents/new?mode=new_connection`, { waitUntil: 'commit' });
  await page.waitForTimeout(2500);
  await shot('02b-connector-catalog');
} catch {}

// search elasticsearch and open its form
const search = page.getByPlaceholder(/search/i).first();
await search.fill('elastic');
await page.waitForTimeout(800);
await shot('03-catalog-elastic');
await page.getByText(/^Elasticsearch$/).first().click();
await page.waitForTimeout(1200);
await shot('04-elastic-form-empty');

// ---- fill the form ----
// name field: first text input with the Sales DB placeholder
await page.locator('input[placeholder*="Sales DB"]').fill(CONN_NAME);
await page.locator('#host').fill(ES_URL);
// port/secure are ignored when host is a full URL; leave defaults.
await page.locator('#api_key').fill(ES_KEY);
await shot('05-elastic-form-filled');

// ---- Test connection ----
await page.getByRole('button', { name: /test/i }).first().click();
// wait for the test result to render
await page.waitForTimeout(6000);
await shot('06-test-connection-result');
const bodyText = await page.locator('body').innerText();
const m = bodyText.match(/Connected to Elasticsearch[^\n]*/i);
console.log('TEST_RESULT:', m ? m[0] : '(no explicit message found)');
if (/failed|error/i.test(bodyText.slice(0, 4000)) && !m) console.log('POSSIBLE_FAILURE_TEXT in page');

// ---- Create the connection (goes to the "indexing" step) ----
const createBtn = page.getByRole('button', { name: /^(create|connect|save)/i }).last();
await createBtn.click();
console.log('clicked create');
// wait for schema discovery to reach a terminal state (Connected/Failed badge)
for (let i = 0; i < 36; i++) {
  await page.waitForTimeout(5000);
  const t = await page.locator('body').innerText();
  if (/Schema discovery/i.test(t) && /Connected|Failed/.test(t)) break;
}
await shot('07-indexing-result');
// click the final "Connect" button in the modal
await page.getByRole('button', { name: /^Connect$/ }).click();
await page.waitForTimeout(2000);
await shot('08-back-on-new-agent');

// ---- name the agent and continue ----
await page.locator('input[placeholder*="Sales"]').first().fill('Elastic Logs');
await page.waitForTimeout(300);
await page.getByRole('button', { name: /save & continue/i }).click();
await page.waitForURL(/agents\/new\/.*\/schema/, { timeout: 30000 }).catch(() => {});
await page.waitForTimeout(4000);
await shot('09-schema-page');
console.log('PAGE_URL:', page.url());
const t2 = await page.locator('body').innerText();
console.log('SNIPPET:', t2.slice(0, 2000).replace(/\n{2,}/g, '\n'));

await ctx.close(); await b.close();
console.log('CONNECT_FLOW_DONE');
