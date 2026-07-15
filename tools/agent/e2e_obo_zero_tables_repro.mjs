// Feedback loop repro: Entra OBO (user_required) Power BI/Fabric connection —
// admin creates connection + agent, Tables step shows ZERO tables even though
// the canonical catalog is indexed; "Reload" silently fails (swallowed 403/500);
// after the user signs in (Microsoft OAuth), tables appear.
//
// Secrets come from env vars ONLY (never committed):
//   BOW_ENTRA_TENANT_ID, BOW_ENTRA_CLIENT_ID, BOW_ENTRA_CLIENT_SECRET   (OAuth app for user sign-in)
//   BOW_PBI_MASTER_CLIENT_ID, BOW_PBI_MASTER_CLIENT_SECRET              (service principal for the catalog)
//   BOW_OAUTH_TEST_DEMO1_EMAIL, BOW_OAUTH_TEST_DEMO1_PASSWORD           (Entra user that signs in)
//
// Usage:
//   tools/agent/boot_stack.sh --dev && cd backend && uv run python ../tools/agent/seed_org.py
//   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tools/agent/e2e_obo_zero_tables_repro.mjs
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');

const BASE = 'http://localhost:3000';
const API = 'http://localhost:8000';
const MEDIA = process.env.MEDIA_DIR || '/tmp/bow-agent/obo-repro-media';
mkdirSync(MEDIA, { recursive: true });

const env = (k) => {
  const v = process.env[k];
  if (!v) { console.error(`missing env var ${k}`); process.exit(2); }
  return v;
};
const TENANT = env('BOW_ENTRA_TENANT_ID');
const OAUTH_CLIENT = env('BOW_ENTRA_CLIENT_ID');
const OAUTH_SECRET = env('BOW_ENTRA_CLIENT_SECRET');
const SP_CLIENT = env('BOW_PBI_MASTER_CLIENT_ID');
const SP_SECRET = env('BOW_PBI_MASTER_CLIENT_SECRET');
const DEMO_EMAIL = env('BOW_OAUTH_TEST_DEMO1_EMAIL');
const DEMO_PW = env('BOW_OAUTH_TEST_DEMO1_PASSWORD');

const executablePath = process.env.CHROMIUM_PATH || '/opt/pw-browsers/chromium';
const browser = await chromium.launch({ executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
let shotIdx = 0;
const shot = async (name) => {
  const f = `${MEDIA}/${String(++shotIdx).padStart(2, '0')}-${name}.png`;
  await page.screenshot({ path: f });
  console.log(`   [shot] ${f}`);
};
const step = (msg) => console.log(`>> ${msg}`);

// ---- API helper (Bearer) for out-of-band assertions --------------------------
let apiToken = '', orgId = '';
async function apiLogin() {
  const r = await fetch(`${API}/api/auth/jwt/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username: 'admin@example.com', password: 'Password123!' }),
  });
  apiToken = (await r.json()).access_token;
  const orgs = await (await fetch(`${API}/api/organizations`, { headers: { Authorization: `Bearer ${apiToken}` } })).json();
  orgId = orgs[0].id;
}
const apiGet = async (path) => {
  const r = await fetch(`${API}/api${path}`, {
    headers: { Authorization: `Bearer ${apiToken}`, 'X-Organization-Id': orgId },
  });
  return { status: r.status, body: await r.json().catch(() => null) };
};

// ---- Microsoft login automation ----------------------------------------------
async function microsoftLogin(email, password) {
  await page.waitForSelector('input[name="loginfmt"]', { timeout: 60000 });
  await shot('ms-login-email');
  await page.fill('input[name="loginfmt"]', email);
  await page.click('#idSIButton9');
  await page.waitForSelector('input[name="passwd"]', { timeout: 60000 });
  await shot('ms-login-password');
  await page.fill('input[name="passwd"]', password);
  await page.click('#idSIButton9');
  // Consent and/or KMSI pages may follow, in any order.
  for (let i = 0; i < 4; i++) {
    await page.waitForTimeout(3000);
    const url = page.url();
    if (!url.includes('login.microsoftonline.com')) break;
    if (await page.locator('#idBtn_Back').count()) {         // "Stay signed in?" -> No
      await shot('ms-kmsi');
      await page.click('#idBtn_Back');
    } else if (await page.locator('#idSIButton9').count()) { // consent "Accept" / other continue
      await shot('ms-consent-or-continue');
      await page.click('#idSIButton9');
    }
  }
}

try {
  await apiLogin();

  // --- 1. Sign in to bagofwords as the org admin -------------------------------
  step('1. sign in as org admin');
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
  await page.fill('#email', 'admin@example.com');
  await page.fill('#password', 'Password123!');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(4000);
  if (page.url().includes('/onboarding')) {
    const skip = page.getByRole('button', { name: /skip/i });
    if (await skip.count()) await skip.first().click();
    await page.waitForTimeout(1500);
  }
  await shot('signed-in');

  // --- 2. New agent page, open Create-connection modal --------------------------
  step('2. open /agents/new and create a Power BI OBO connection');
  await page.goto(`${BASE}/agents/new`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  const createLink = page.getByRole('button', { name: /create new connection/i });
  if (await createLink.count()) await createLink.click();  // modal may auto-open with 0 connections
  await page.waitForTimeout(1500);
  await shot('add-connection-modal');

  const search = page.locator('input[placeholder*="earch"]').first();
  await search.fill('Power BI');
  await page.waitForTimeout(800);
  await page.getByText('Power BI', { exact: true }).first().click();
  await page.waitForTimeout(1200);

  // --- 3. Fill SP creds, enable "Require user authentication", OAuth app creds --
  step('3. fill credentials + require user auth (OBO)');
  await page.fill('#tenant_id', TENANT);
  await page.fill('#client_id', SP_CLIENT);
  await page.fill('#client_secret', SP_SECRET);
  // toggle "Require user authentication"
  const toggleRow = page.locator('div.flex.items-center', { hasText: 'Require user authentication' }).last();
  await toggleRow.locator('button').first().click();
  await page.waitForTimeout(600);
  // OAuth app registration used for user sign-in (different from the SP)
  await page.fill('#oauth_client_id', OAUTH_CLIENT);
  await page.fill('#oauth_client_secret', OAUTH_SECRET);
  await shot('connection-form-filled-obo');

  step('3b. test connection');
  await page.getByRole('button', { name: /test connection/i }).click();
  await page.waitForSelector('text=/Connected successfully|success/i', { timeout: 120000 });
  await shot('connection-test-passed');

  step('3c. save connection');
  await page.getByRole('button', { name: /save and continue/i }).click();
  // The modal's indexing step runs SP-seeded schema discovery and reports the
  // table count ("Discovered N tables") — proof the canonical catalog is full.
  await page.waitForSelector('text=/Discovered \\d+ tables/i', { timeout: 180000 });
  await page.waitForTimeout(1000);
  await shot('connection-created-catalog-discovered');
  await page.getByRole('button', { name: /^connect$/i }).click();
  await page.waitForTimeout(2500);

  // --- 4. Create the agent from the connection ---------------------------------
  step('4. name agent + save & continue');
  const conns = await apiGet('/connections');
  const conn = conns.body.find((c) => c.type === 'powerbi');
  console.log(`   connection id=${conn.id} auth_policy=${conn.auth_policy} modes=${JSON.stringify(conn.allowed_user_auth_modes)}`);

  await page.fill('input[placeholder*="Sales"]', 'PBI OBO Demo');
  // connection should be auto-selected after modal create; if not, pick it
  const selector = page.getByText('Select connections');
  if (await selector.count()) {
    await selector.click();
    await page.waitForTimeout(500);
    await page.getByText(conn.name).first().click();
    await page.keyboard.press('Escape');
  }
  // turn OFF "Use LLM to learn agent" (no LLM configured in this sandbox)
  const llmRow = page.locator('div.flex.items-center', { hasText: 'Use LLM to learn agent' }).last();
  const llmBtn = llmRow.locator('button').first();
  if (await llmBtn.count() && (await llmBtn.getAttribute('aria-checked')) === 'true') await llmBtn.click();
  await shot('agent-form-filled');
  await page.getByRole('button', { name: /save & continue/i }).click();
  await page.waitForURL('**/agents/new/*/schema', { timeout: 60000 });
  const dsId = page.url().match(/agents\/new\/([^/]+)\/schema/)[1];
  console.log(`   data source id=${dsId}`);
  await page.waitForTimeout(3000);
  await shot('schema-step-initial');

  // --- 5. Wait for background catalog indexing to finish, prove catalog is full -
  step('5. wait for canonical catalog indexing (SP-seeded) to complete');
  for (let i = 0; i < 60; i++) {
    const idx = await apiGet(`/connections/${conn.id}/indexing`);
    const st = idx.body?.status || 'none';
    if (i % 5 === 0) console.log(`   indexing status: ${st} ${idx.body?.phase || ''} ${idx.body?.progress_done || 0}/${idx.body?.progress_total || 0}`);
    if (st === 'completed' || st === 'failed' || st === 'none') break;
    await new Promise((r) => setTimeout(r, 5000));
  }
  const catalog = await apiGet(`/connections/${conn.id}/tables`);
  console.log(`   CANONICAL CATALOG TABLES: ${Array.isArray(catalog.body) ? catalog.body.length : 'err'} -> ${(catalog.body || []).map((t) => t.name).join(', ')}`);

  // --- 6. The bug: reload the schema step — still ZERO tables -------------------
  step('6. reload schema page: tables selector still shows zero');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  await shot('schema-step-zero-tables-after-indexing');

  // capture the swallowed refresh_schema failure
  step('6b. click Reload — the refresh_schema call fails silently');
  const respPromise = page.waitForResponse((r) => r.url().includes('refresh_schema'), { timeout: 30000 }).catch(() => null);
  const reloadBtn = page.getByRole('button', { name: /reload/i }).first();
  if (await reloadBtn.count()) await reloadBtn.click();
  else await page.locator('button[title*="eload"], button:has(svg.animate-none)').first().click();
  const resp = await respPromise;
  if (resp) console.log(`   /refresh_schema HTTP ${resp.status()}: ${(await resp.text()).slice(0, 300)}`);
  await page.waitForTimeout(3000);
  await shot('after-reload-still-zero');

  // --- 7. Save & continue through the wizard ------------------------------------
  step('7. save & continue (empty selection) -> context -> finish');
  await page.getByRole('button', { name: /save & continue/i }).first().click();
  await page.waitForTimeout(4000);
  await shot('context-step');
  const finish = page.getByRole('button', { name: /finish|done|save|continue/i }).first();
  if (await finish.count()) await finish.click();
  await page.waitForTimeout(4000);
  await shot('after-wizard');

  // --- 8. Agent page: click Connect — real OAuth redirect to Microsoft ----------
  step('8. open agent page, click Connect (starts the OAuth redirect)');
  await page.goto(`${BASE}/agents/${dsId}`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  await shot('agent-page-before-signin');

  await page.getByRole('button', { name: 'Connect', exact: true }).first().click();
  step('8b. Microsoft login as demo user');
  await page.waitForURL('**login.microsoftonline.com**', { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(2000);
  await shot('redirect-to-microsoft-login');
  // In cloud sandboxes the egress proxy rejects Chromium's TLS handshake, so the
  // hosted Microsoft page cannot render here — complete the sign-in with the
  // equivalent delegated-token path instead (see e2e_obo_signin_ropc.py), then
  // run e2e_obo_after_signin.mjs. On a normal network the login form works:
  if (page.url().includes('login.microsoftonline.com') && (await page.locator('input[name="loginfmt"]').count())) {
    await microsoftLogin(DEMO_EMAIL, DEMO_PW);
    await page.waitForURL(`${BASE}/**`, { timeout: 90000 });
    await page.waitForTimeout(4000);
    await shot('back-after-oauth');

    // --- 9. Tables now appear ----------------------------------------------------
    step('9. same wizard tables step after sign-in: tables are now visible');
    await page.goto(`${BASE}/agents/new/${dsId}/schema`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(5000);
    await shot('tables-step-after-signin');

    const pag = await apiGet(`/data_sources/${dsId}/full_schema?page=1&page_size=50`);
    console.log(`   FULL_SCHEMA AS ADMIN AFTER SIGN-IN: total_tables=${pag.body?.total_tables} rows=${(pag.body?.tables || []).length}`);
  } else {
    console.log('   Microsoft page unreachable in this sandbox — finish with e2e_obo_signin_ropc.py + e2e_obo_after_signin.mjs');
  }

  console.log('DONE');
} catch (e) {
  console.error('FAILED:', e);
  await shot('failure');
  console.log('page url:', page.url());
  process.exitCode = 1;
} finally {
  await browser.close();
}
