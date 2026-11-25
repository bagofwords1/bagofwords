// Drives the BOW UI to exercise the Siemens Teamcenter connector against the mock Teamcenter
// (tools/teamcenter/mock_teamcenter_server.py) and capture evidence. See
// docs/feedback-loops/teamcenter-connector.md.
//
// @playwright/test resolves from the script's own directory (ESM), so run a copy inside frontend/:
//   node teamcenter_ui_flow.mjs connect <outdir>                       connect form + test + tables + agent
//   node teamcenter_ui_flow.mjs chat <outdir> "<prompt>" <tag> [max_wait_s]
//   node teamcenter_ui_flow.mjs sso <outdir> <connection_id>           a member's Teamcenter SSO sign-in
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const [MODE, OUTDIR, ...rest] = process.argv.slice(2);
const OUT = OUTDIR || '../media/pr/teamcenter-connector';
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const TC_URL = process.env.TC_URL || 'http://127.0.0.1:8099/tc';
const TC_USER = process.env.TC_USER || 'infodba';
const TC_PASSWORD = process.env.TC_PASSWORD || 'infodba-Secr3t';
const TYPES = process.env.TC_TYPES || 'A2_PartRevision, ItemRevision, ChangeRequestRevision, ChangeNoticeRevision, ProblemReportRevision';
const EXEC = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({ executablePath: EXEC });
const ctx = await browser.newContext({ viewport: { width: 1512, height: 1000 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (name, full = false) => {
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: full });
  console.log('shot', name);
};

await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'load', timeout: 180000 });
await page.locator('#email').fill(EMAIL, { timeout: 120000 });
await page.locator('#password').fill(PASSWORD);
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2500);
try { await page.getByText(/skip onboarding/i).click({ timeout: 3000 }); await page.waitForTimeout(1500); } catch {}

const fill = async (label, value) => {
  const byLabel = page.getByLabel(label, { exact: false });
  if (await byLabel.count()) { await byLabel.first().fill(value); return true; }
  const byPh = page.getByPlaceholder(label);
  if (await byPh.count()) { await byPh.first().fill(value); return true; }
  console.log('FIELD NOT FOUND', String(label));
  return false;
};

if (MODE === 'connect') {
  await page.goto(`${BASE}/agents/new`, { waitUntil: 'commit' });
  await page.waitForTimeout(2500);
  const portal = page.locator('#headlessui-portal-root');
  if (!(await portal.count()) || !(await portal.innerText().catch(() => '')).trim()) {
    await page.getByText(/create new connection/i).first().click({ timeout: 15000 });
  }
  await page.waitForTimeout(1500);
  const search = page.getByPlaceholder(/search/i).first();
  if (await search.count()) { await search.fill('plm'); await page.waitForTimeout(600); }
  await shot('01-catalog-teamcenter-tile');
  const tile = page.getByRole('button', { name: /Siemens Teamcenter/i }).first();
  await tile.evaluate((el) => el.scrollIntoView({ block: 'center' })).catch(() => {});
  await tile.click({ timeout: 8000 }).catch(async () => { await tile.click({ force: true }); });
  await page.waitForTimeout(1500);
  await shot('02-connect-form-empty', true);

  const nameField = page.getByPlaceholder(/name/i).first();
  if (await nameField.count()) await nameField.fill('Teamcenter (Pump program)').catch(() => {});
  await fill(/teamcenter url/i, TC_URL);
  await fill(/object types/i, TYPES);
  await page.getByRole('button', { name: /^advanced$/i }).first().click().catch(() => {});
  await page.waitForTimeout(500);
  // The lab's mock speaks plain HTTP: switch on "Allow HTTP (test labs only)" under Advanced settings.
  const allowHttp = page.locator('div.mb-2', { has: page.locator('label', { hasText: /allow http/i }) })
    .locator('[role=switch], button').first();
  await allowHttp.scrollIntoViewIfNeeded().catch(() => {});
  await allowHttp.click().catch((e) => console.log('ALLOW_HTTP not toggled', String(e).slice(0, 120)));
  await shot('03a-advanced-settings', true);
  await fill(/user id/i, TC_USER);
  await fill(/^password/i, TC_PASSWORD);
  await shot('03-connect-form-filled', true);

  await page.getByRole('button', { name: /^connect$/i }).first().click({ timeout: 15000 });
  let status = '';
  for (let i = 0; i < 60; i++) {
    await page.waitForTimeout(2000);
    status = await page.locator('body').innerText().catch(() => '');
    if (/discovered \d+ tables|Connected to Teamcenter|did not|failed|error/i.test(status) && i > 1) break;
  }
  await shot('04-connection-status');
  const m = status.match(/(Connected to Teamcenter[^\n]*|[^\n]*refused[^\n]*|[^\n]*did not answer[^\n]*)/);
  console.log('TEST_RESULT:', m ? m[1] : '(no message found)');
  const d = status.match(/discovered \d+ tables/i);
  console.log('DISCOVERY:', d ? d[0] : '(none)');
  if (!d) { console.log('STATUS_PANEL:', status.slice(0, 2000)); await ctx.close(); await browser.close(); process.exit(1); }
  await page.getByRole('button', { name: /^(done|connect)$/i }).first().click({ timeout: 15000 });
  await page.waitForTimeout(2500);
  const agentName = page.getByPlaceholder(/sales, marketing/i).first();
  if (await agentName.count()) await agentName.fill('PLM (Teamcenter)');
  await page.getByRole('button', { name: /save & continue|save and continue/i }).first().click({ timeout: 15000 }).catch(() => {});
  for (let i = 0; i < 24; i++) {
    await page.waitForTimeout(5000);
    const t = await page.locator('body').innerText().catch(() => '');
    if (/a2_part_revision/.test(t)) break;
  }
  await shot('05-select-tables');
  const tablesTxt = await page.locator('body').innerText().catch(() => '');
  console.log('TABLES_SEEN:', ['a2_part_revision', 'item_revision', 'change_notice_revision', 'change_request_revision',
    'problem_report_revision', 'bom', 'where_used', 'change_items', 'revision_rules'].filter((t) => tablesTxt.includes(t)).join(','));
  for (const label of [/select all/i, /all tables/i]) {
    const ctl = page.getByRole('button', { name: label }).first();
    if (await ctl.count()) { await ctl.click().catch(() => {}); break; }
    const cb = page.getByLabel(label).first();
    if (await cb.count()) { await cb.click().catch(() => {}); break; }
  }
  await page.waitForTimeout(800);
  await page.getByRole('button', { name: /save & continue|save and continue|continue|next|finish/i }).first().click({ timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(4000);
  console.log('url after tables:', page.url());
  await shot('06-after-tables');
} else if (MODE === 'chat') {
  const PROMPT = rest[0];
  const TAG = rest[1] || 'chat';
  const WAIT = parseInt(rest[2] || '420', 10);
  await page.goto(`${BASE}/`, { waitUntil: 'commit' });
  await page.waitForTimeout(3500);
  const box = page.locator('[contenteditable=true]').first();
  await box.click();
  await page.keyboard.type(PROMPT, { delay: 4 });
  await page.waitForTimeout(500);
  const sendBtn = page.locator('button.rounded-full').last();
  await sendBtn.click({ timeout: 8000 }).catch(async () => { await box.click(); await page.keyboard.press('Enter'); });
  await page.waitForTimeout(3000);
  console.log('url after submit:', page.url());
  let lastLen = 0, stableFor = 0;
  for (let i = 0; i < Math.ceil(WAIT / 10); i++) {
    await page.waitForTimeout(10000);
    const t = await page.locator('body').innerText().catch(() => '');
    process.stdout.write(`.${t.length}`);
    if (t.length === lastLen) stableFor += 10; else { stableFor = 0; lastLen = t.length; }
    if (stableFor >= 50 && t.length > 1500) break;
  }
  console.log('\nfinal url:', page.url());
  await shot(`10-${TAG}-final`);
  await shot(`11-${TAG}-final-full`, true);
  const finalText = await page.locator('body').innerText().catch(() => '');
  console.log('BODY_TAIL:\n', finalText.slice(-3500));
} else if (MODE === 'sso') {
  // A member signs in with Teamcenter SSO: BOW -> TcSS login page -> back to BOW's callback -> connected.
  // The sign-in starts with the same request the Connect button sends (GET .../oauth/authorize); its
  // signed-state cookie goes into this browser, which then does the real TcSS round trip.
  const CONN = rest[0];
  const TCSS_USER = process.env.TCSS_USER || 'jdoe';
  const TCSS_PASSWORD = process.env.TCSS_PASSWORD || 'jdoe-pw';
  const login = await fetch(`${BASE}/api/auth/jwt/login`, { method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username: EMAIL, password: PASSWORD }) });
  const jwt = (await login.json()).access_token;
  const orgs = await (await fetch(`${BASE}/api/organizations`, { headers: { Authorization: `Bearer ${jwt}` } })).json();
  const start = await fetch(`${BASE}/api/connections/${CONN}/oauth/authorize?return_to=/agents`,
    { headers: { Authorization: `Bearer ${jwt}`, 'X-Organization-Id': orgs[0].id } });
  const { authorization_url: url } = await start.json();
  const setCookie = start.headers.get('set-cookie') || '';
  const state = (setCookie.match(/conn_tcss_state=([^;]+)/) || [])[1];
  console.log('authorization_url:', url, 'state cookie:', state ? 'set' : 'MISSING');
  await ctx.addCookies([{ name: 'conn_tcss_state', value: state, url: `${BASE}/api/connections` }]);
  await page.goto(url, { waitUntil: 'load' });
  await shot('21-tcss-login-page');
  await page.locator('input[name=username]').fill(TCSS_USER);
  await page.locator('input[name=password]').fill(TCSS_PASSWORD);
  await shot('21b-tcss-login-filled');
  await Promise.all([
    page.waitForURL((u) => u.origin === new URL(BASE).origin && u.pathname.startsWith('/agents'), { timeout: 60000 }),
    page.locator('button[type=submit]').click(),
  ]);
  await page.waitForTimeout(5000);
  console.log('landed:', page.url());
  await shot('22-sso-signed-in');
  const status = await (await fetch(`${BASE}/api/connections/${CONN}`, {
    headers: { Authorization: `Bearer ${jwt}`, 'X-Organization-Id': orgs[0].id } })).json();
  console.log('USER_STATUS:', JSON.stringify(status.user_status || {}));
} else {
  console.error('usage: node teamcenter_ui_flow.mjs connect|chat|sso <outdir> ...');
}
await ctx.close(); await browser.close(); console.log('TEAMCENTER_FLOW_DONE');
