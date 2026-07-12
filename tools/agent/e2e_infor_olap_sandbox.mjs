// End-to-end: create an Infor OLAP connection through the real bagofwords UI
// against a local Mondrian XMLA sandbox (xmondrian @ :18080), test it, save it,
// watch indexing complete, and screenshot every milestone.
//
// Usage (any cwd):
//   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tools/agent/e2e_infor_olap_sandbox.mjs
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';

// Resolve @playwright/test from frontend/node_modules so the script runs
// from any cwd (ESM resolves imports relative to this file, not $PWD).
const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');

const BASE = 'http://localhost:3000';
const XMLA_URL = 'http://localhost:18080/xmondrian/xmla';
const MEDIA = process.env.MEDIA_DIR || '/tmp/bow-agent/e2e-media';
mkdirSync(MEDIA, { recursive: true });

// Cloud sandboxes pin browsers at /opt/pw-browsers; the version-agnostic
// 'chromium' symlink avoids @playwright/test build-number mismatches.
const executablePath = process.env.CHROMIUM_PATH || '/opt/pw-browsers/chromium';
const browser = await chromium.launch({ executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const shot = (name) => page.screenshot({ path: `${MEDIA}/${name}.png` });
const step = (msg) => console.log(`>> ${msg}`);

try {
  // --- 1. Login ---------------------------------------------------------------
  step('login');
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
  await page.fill('#email', 'admin@example.com');
  await page.fill('#password', 'Password123!');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(4000);

  // Dismiss onboarding if it intercepts
  if (page.url().includes('/onboarding')) {
    step('skipping onboarding');
    const skip = page.getByRole('button', { name: 'Skip onboarding' });
    if (await skip.isVisible({ timeout: 10000 }).catch(() => false)) {
      await skip.click();
      await page.waitForTimeout(2000);
    }
  }

  // --- 2. Agents explorer → Connect data --------------------------------------
  step('open /agents');
  await page.goto(`${BASE}/agents`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  await shot('01-agents-page');

  step('open Add Connection modal');
  const connectBtn = page.getByRole('button', { name: /connect data/i }).first();
  await connectBtn.waitFor({ state: 'visible', timeout: 20000 });
  await connectBtn.click();

  // --- 3. Pick Infor OLAP ------------------------------------------------------
  step('search + select Infor OLAP');
  const search = page.getByPlaceholder(/search data sources/i);
  await search.waitFor({ state: 'visible', timeout: 15000 });
  await search.fill('Infor');
  await page.waitForTimeout(800);
  await shot('02-picker-infor');
  await page.getByText('Infor OLAP', { exact: true }).first().click();
  await page.waitForTimeout(1500);

  // --- 4. Fill the connection form ---------------------------------------------
  step('fill form');
  await page.getByPlaceholder(/Sales DB/).fill('Infor OLAP (sandbox)');
  await page.locator('#host').fill(XMLA_URL);
  // catalog left blank — discover all. Fill credentials:
  await page.locator('#username').fill('demo');
  await page.locator('#password').fill('demo');
  await shot('03-form-filled');

  // --- 5. Test Connection -------------------------------------------------------
  step('test connection');
  await page.getByRole('button', { name: 'Test Connection' }).click();
  const okMsg = page.getByText(/Connected successfully/i);
  await okMsg.waitFor({ state: 'visible', timeout: 30000 });
  console.log('TEST CONNECTION =>', (await okMsg.textContent())?.trim());
  await shot('04-test-connection-success');

  // --- 6. Save and Continue → indexing ------------------------------------------
  step('save and continue');
  await page.getByRole('button', { name: 'Save and Continue' }).click();

  step('wait for schema indexing to complete');
  // Indexing step UI; wait until completed state or the finish button enables.
  const completed = page.getByText(/completed|indexed|done/i).first();
  await completed.waitFor({ state: 'visible', timeout: 120000 }).catch(() => {});
  await page.waitForTimeout(3000);
  await shot('05-indexing');

  // Click the finish/connect button (whatever closes the modal)
  const finish = page.getByRole('button', { name: /^(connect|done|finish|close)/i }).last();
  if (await finish.isVisible().catch(() => false)) await finish.click();
  await page.waitForTimeout(3000);
  await shot('06-after-save');

  // --- 7. Verify via API: schema tables discovered -------------------------------
  step('verify schema via API');
  const api = await page.request.post('http://localhost:8000/api/auth/jwt/login', {
    form: { username: 'admin@example.com', password: 'Password123!' },
  });
  const token = (await api.json()).access_token;
  const orgs = await (await page.request.get('http://localhost:8000/api/organizations', {
    headers: { Authorization: `Bearer ${token}` } })).json();
  const orgId = orgs[0].id;
  const H = { Authorization: `Bearer ${token}`, 'X-Organization-Id': orgId };
  const conns = await (await page.request.get('http://localhost:8000/api/connections', { headers: H })).json();
  const infor = conns.filter((c) => c.type === 'infor_olap').pop();
  console.log('CONNECTION =>', infor?.name, infor?.type, infor?.id);
  const indexing = await (await page.request.get(
    `http://localhost:8000/api/connections/${infor.id}/indexing`, { headers: H })).json();
  console.log('INDEXING =>', indexing.status, 'tables:', indexing.stats?.table_count);
  if (indexing.status !== 'completed' || !(indexing.stats?.table_count > 0)) {
    throw new Error(`indexing not completed with tables: ${JSON.stringify(indexing.stats)}`);
  }

  console.log('E2E RESULT: PASS');
} catch (e) {
  await shot('99-failure');
  console.error('E2E RESULT: FAIL —', e.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
