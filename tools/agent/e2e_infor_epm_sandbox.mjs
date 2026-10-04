// End-to-end: create an Infor EPM (Application Engine) connection through the
// real bagofwords UI against the mock Application Engine (tools/agent/
// mock_infor_epm_server.py @ :8765), test it, save it, watch indexing complete,
// then ask the agent a question answered from the mock cube — screenshotting
// every milestone.
//
// Usage (any cwd, stack + mock + Haiku LLM already up):
//   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tools/agent/e2e_infor_epm_sandbox.mjs
import { mkdirSync, writeFileSync, appendFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');

const BASE = 'http://localhost:3000';
const API = 'http://localhost:8000';
const EPM_API_URL = process.env.EPM_API_URL || 'http://localhost:8765/api/rest/BowService/v1';
const EPM_TOKEN_URL = process.env.EPM_TOKEN_URL || 'http://localhost:8765/token';
const MEDIA = process.env.MEDIA_DIR || '/tmp/bow-agent/epm-media';
const PROMPT = process.env.PROMPT || 'Using the Sales cube, what is the total Revenue for each Region in 2024? Show a table.';
mkdirSync(MEDIA, { recursive: true });
const httpLog = `${MEDIA}/api-responses.jsonl`;
writeFileSync(httpLog, '');

const executablePath = process.env.CHROMIUM_PATH || '/opt/pw-browsers/chromium';
const browser = await chromium.launch({ executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on('response', (r) => {
  if (r.url().includes('/api/')) appendFileSync(httpLog, JSON.stringify({ url: r.url(), status: r.status() }) + '\n');
});
const shot = (name) => page.screenshot({ path: `${MEDIA}/${name}.png` });
const step = (msg) => console.log(`>> ${msg}`);

try {
  // --- 1. Login ---------------------------------------------------------------
  step('login');
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
  // Dev-mode Nuxt can take a while to hydrate on a cold route.
  await page.locator('#email').waitFor({ state: 'visible', timeout: 120000 });
  await page.fill('#email', 'admin@example.com');
  await page.fill('#password', 'Password123!');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(4000);
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
  const connectBtn = page.getByRole('button', { name: /connect data/i }).first();
  await connectBtn.waitFor({ state: 'visible', timeout: 90000 });
  await connectBtn.click();

  // --- 3. Pick Infor EPM -------------------------------------------------------
  step('search + select Infor EPM');
  const search = page.getByPlaceholder(/search data sources/i);
  await search.waitFor({ state: 'visible', timeout: 15000 });
  await search.fill('Infor');
  await page.waitForTimeout(800);
  await shot('01-picker-infor');
  await page.getByText('Infor EPM (Application Engine)', { exact: true }).first().click();
  await page.waitForTimeout(1500);
  await shot('02-form-empty');

  // --- 4. Fill the connection form ---------------------------------------------
  step('fill form');
  await page.getByPlaceholder(/Sales DB/).fill('Infor EPM (mock farm)');
  await page.locator('#api_url').fill(EPM_API_URL);
  await page.locator('#olap_database').fill('DEMO_OLAP');
  await page.locator('#gateway_token_url').fill(EPM_TOKEN_URL);
  await page.locator('#gateway_client_id').fill('demo-client');
  await page.locator('#gateway_client_secret').fill('demo-secret');
  await shot('03-form-filled');
  await page.screenshot({ path: `${MEDIA}/03b-form-filled-full.png`, fullPage: true });

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
  const completed = page.getByText(/completed|indexed|done/i).first();
  await completed.waitFor({ state: 'visible', timeout: 120000 }).catch(() => {});
  await page.waitForTimeout(3000);
  await shot('05-indexing');
  const finish = page.getByRole('button', { name: /^(connect|done|finish|close)/i }).last();
  if (await finish.isVisible().catch(() => false)) await finish.click();
  await page.waitForTimeout(3000);
  await shot('06-after-save');

  // --- 7. Verify via API: schema tables discovered -------------------------------
  step('verify schema via API');
  const login = await page.request.post(`${API}/api/auth/jwt/login`, {
    form: { username: 'admin@example.com', password: 'Password123!' },
  });
  const token = (await login.json()).access_token;
  const orgs = await (await page.request.get(`${API}/api/organizations`, {
    headers: { Authorization: `Bearer ${token}` } })).json();
  const H = { Authorization: `Bearer ${token}`, 'X-Organization-Id': orgs[0].id };
  const conns = await (await page.request.get(`${API}/api/connections`, { headers: H })).json();
  const epm = conns.filter((c) => c.type === 'infor_epm').pop();
  console.log('CONNECTION =>', epm?.name, epm?.type, epm?.id, 'is_active:', epm?.is_active);
  let indexing = null;
  for (let i = 0; i < 60; i++) {
    indexing = await (await page.request.get(`${API}/api/connections/${epm.id}/indexing`, { headers: H })).json();
    if (indexing.status === 'completed' || indexing.status === 'failed') break;
    await page.waitForTimeout(2000);
  }
  console.log('INDEXING =>', indexing.status, 'tables:', indexing.stats?.table_count);
  if (indexing.status !== 'completed' || !(indexing.stats?.table_count > 0)) {
    throw new Error(`indexing not completed with tables: ${JSON.stringify(indexing)}`);
  }
  // --- 7b. Wrap the connection in an agent (what the "New agent" wizard posts) ----
  step('create agent over the connection via API');
  const existing = await (await page.request.get(`${API}/api/data_sources`, { headers: H })).json();
  let agent = (Array.isArray(existing) ? existing : []).find((d) => d.name === 'Infor EPM Agent');
  if (!agent) {
    const created = await page.request.post(`${API}/api/data_sources`, {
      headers: H, data: { name: 'Infor EPM Agent', connection_ids: [epm.id] },
    });
    console.log('CREATE AGENT =>', created.status());
    agent = await created.json();
  }
  console.log('AGENT =>', agent?.name, agent?.id);
  // Linking an existing connection leaves its tables inactive until the
  // wizard's table-selection step activates them — same call it makes.
  const act = await page.request.post(`${API}/api/data_sources/${agent.id}/bulk_update_tables`, {
    headers: H, data: { action: 'activate', filter: {} },
  });
  console.log('ACTIVATE TABLES =>', act.status(), (await act.text()).slice(0, 200));

  // --- 8. Ask the agent a question answered from the mock cube --------------------
  step('open home and select the data source');
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  // The prompt box's agent-scope picker (DataSourceSelector.vue).
  await page.locator('[data-testid="agent-scope-trigger"]').first().click({ timeout: 15000 });
  await page.waitForTimeout(800);
  await shot('07a-agent-picker-open');
  const option = page.locator('[data-testid="agent-scope-option"]', { hasText: /Infor EPM Agent/i }).first();
  if (await option.isVisible().catch(() => false)) {
    await option.click();
  } else {
    console.log('agent option not listed — leaving auto scope');
  }
  await page.waitForTimeout(500);
  await page.mouse.click(60, 60);
  await page.waitForTimeout(500);
  await shot('07-home-source-selected');

  step('type + send the prompt');
  const box = page.locator('[contenteditable="true"]').first();
  await box.click();
  await page.keyboard.type(PROMPT, { delay: 5 });
  await page.waitForTimeout(600);
  await shot('08-prompt-typed');
  const sendBtn = page.locator('button[class*="rounded-full"]').last();
  await sendBtn.click({ timeout: 8000 }).catch(async () => { await box.click(); await page.keyboard.press('Enter'); });
  await page.waitForTimeout(4000);
  for (let i = 0; i < 20 && !/\/reports\//.test(page.url()); i++) await page.waitForTimeout(1000);
  console.log('REPORT URL =>', page.url());
  const reportId = (page.url().match(/\/reports\/([^/?#]+)/) || [])[1];
  if (!reportId) {
    await shot('99-no-report');
    throw new Error('prompt was not submitted — no /reports/{id} navigation (is an agent selected and a model enabled?)');
  }

  step('wait for the agent to finish');
  const deadline = Date.now() + 240000;
  while (Date.now() < deadline) {
    await page.waitForTimeout(5000);
    const stop = await page.locator('[data-testid="stop-button"]').count();
    const thinking = await page.getByText(/Thinking/i).count();
    if (stop === 0 && thinking === 0) break;
  }
  await page.waitForTimeout(2000);
  await shot('09-agent-answer');
  await page.screenshot({ path: `${MEDIA}/09b-agent-answer-full.png`, fullPage: true });

  // --- 9. Verify the turn at the API layer ----------------------------------------
  if (reportId) {
    const comps = await (await page.request.get(`${API}/api/reports/${reportId}/completions`, { headers: H })).json().catch(() => null);
    const list = Array.isArray(comps) ? comps : (comps?.items || comps?.completions || []);
    const statuses = list.map((c) => `${c.role}:${c.status}`);
    console.log('COMPLETIONS =>', statuses.join(', '));
    const bodyText = await page.locator('body').innerText();
    const hasRegion = /North|South|East|West/.test(bodyText);
    console.log('ANSWER MENTIONS REGIONS =>', hasRegion);
    if (!hasRegion) throw new Error('agent answer does not mention the mock regions');
  }

  console.log('E2E RESULT: PASS');
} catch (e) {
  await shot('99-failure');
  console.error('E2E RESULT: FAIL —', e.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
