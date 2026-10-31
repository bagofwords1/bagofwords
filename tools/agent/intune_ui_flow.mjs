// Drives the BOW UI to exercise the Microsoft Intune connector against the
// Graph stand-in (tools/intune/mock_graph.py) and capture evidence.
//
// The backend must have been started with BOW_GRAPH_BASE_URL /
// BOW_GRAPH_TOKEN_BASE_URL pointing at the mock (see
// docs/feedback-loops/intune-connector.md). @playwright/test resolves from the
// script's own directory (ESM), so run from a copy inside frontend/.
//
//   node intune_ui_flow.mjs connect <outdir> [client_id] [secret]   connect + test + tables
//   node intune_ui_flow.mjs chat <outdir> "<prompt>" <tag> [max_wait_s]
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const [MODE, OUTDIR, ...rest] = process.argv.slice(2);
const OUT = OUTDIR || '../media/pr/intune-connector';
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const TENANT = process.env.INTUNE_TENANT_ID || '11111111-1111-1111-1111-111111111111';
const CLIENT_ID = rest[0] || process.env.INTUNE_CLIENT_ID || 'intune-reader';
const CLIENT_SECRET = rest[1] || process.env.INTUNE_CLIENT_SECRET || 'Reader123!';
const EXEC = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({ executablePath: EXEC });
const ctx = await browser.newContext({ viewport: { width: 1512, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (name, full = false) => {
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: full });
  console.log('shot', name);
};

// ── sign in ────────────────────────────────────────────────────────────────
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'commit' });
await page.locator('#email').fill(EMAIL);
await page.locator('#password').fill(PASSWORD);
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2500);
try { await page.getByText(/skip onboarding/i).click({ timeout: 3000 }); await page.waitForTimeout(1500); } catch {}

if (MODE === 'connect') {
  await page.goto(`${BASE}/agents/new`, { waitUntil: 'commit' });
  await page.waitForTimeout(2500);
  const portal = page.locator('#headlessui-portal-root');
  if (!(await portal.count()) || !(await portal.innerText().catch(() => '')).trim()) {
    await page.getByText(/create new connection/i).first().click({ timeout: 15000 });
  }
  await page.waitForTimeout(1500);
  try {
    const search = page.getByPlaceholder(/search/i).first();
    if (await search.count()) { await search.fill('intune'); await page.waitForTimeout(600); }
  } catch {}
  await shot('01-catalog-intune-tile');
  const tile = page.getByRole('button', { name: /Microsoft Intune/i }).first();
  await tile.evaluate((el) => el.scrollIntoView({ block: 'center' })).catch(() => {});
  await page.waitForTimeout(400);
  await tile.click({ timeout: 8000 }).catch(async () => { await tile.click({ force: true }); });
  await page.waitForTimeout(1500);
  await shot('02-connect-form-empty');

  const fill = async (label, value) => {
    const byLabel = page.getByLabel(label, { exact: false });
    if (await byLabel.count()) { await byLabel.first().fill(value); return true; }
    const byPh = page.getByPlaceholder(label);
    if (await byPh.count()) { await byPh.first().fill(value); return true; }
    console.log('FIELD NOT FOUND', String(label));
    return false;
  };
  const nameField = page.getByPlaceholder(/name/i).first();
  if (await nameField.count()) await nameField.fill('Intune (Contoso)').catch(() => {});
  await fill(/tenant id/i, TENANT);
  await fill(/client id/i, CLIENT_ID);
  await fill(/client secret/i, CLIENT_SECRET);
  await shot('03-connect-form-filled');

  // "Connect" runs the connection check, then schema discovery, in the
  // status panel on the right.
  await page.getByRole('button', { name: /^connect$/i }).first().click({ timeout: 15000 });
  let status = '';
  for (let i = 0; i < 60; i++) {
    await page.waitForTimeout(2000);
    status = await page.locator('body').innerText().catch(() => '');
    if (/discovered \d+ tables|Token works|Could not get|no active Intune|failed|error/i.test(status) && i > 1) break;
  }
  await shot('04-connection-status');
  const m = status.match(/(Connected to Intune[^\n]*|Token works[^\n]*|Could not get[^\n]*|[^\n]*no active Intune[^\n]*)/);
  console.log('TEST_RESULT:', m ? m[1] : '(no message found)');
  const d = status.match(/discovered \d+ tables/i);
  console.log('DISCOVERY:', d ? d[0] : '(none)');
  if (process.env.STOP_AFTER_TEST || !d) { console.log('STATUS_PANEL:', status.slice(status.indexOf('Connection status'), status.indexOf('Connection status') + 600)); await ctx.close(); await browser.close(); process.exit(0); }
  await shot('05-schema-discovery');
  await page.getByRole('button', { name: /^(done|connect)$/i }).first().click({ timeout: 15000 });
  await page.waitForTimeout(2500);
  const agentName = page.getByPlaceholder(/sales, marketing/i).first();
  if (await agentName.count()) await agentName.fill('Intune (Contoso)');
  await page.getByRole('button', { name: /save & continue|save and continue/i }).first().click({ timeout: 15000 }).catch(() => {});
  for (let i = 0; i < 24; i++) {
    await page.waitForTimeout(5000);
    const t = await page.locator('body').innerText().catch(() => '');
    if (/managed_devices/.test(t)) break;
  }
  console.log('url at tables step:', page.url());
  await shot('06-select-tables');
  const tablesTxt = await page.locator('body').innerText().catch(() => '');
  console.log('TABLES_SEEN:', ['managed_devices','compliance_policies','compliance_policy_device_statuses','configuration_profiles','mobile_apps','detected_apps','autopilot_devices'].filter((t) => tablesTxt.includes(t)).join(','));
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
  await shot('07-after-tables');
} else if (MODE === 'chat') {
  const PROMPT = rest[0] || 'What does How many Intune devices are non-compliant, by operating system?';
  const TAG = rest[1] || 'chat';
  const WAIT = parseInt(rest[2] || '480', 10);

  await page.goto(`${BASE}/`, { waitUntil: 'commit' });
  await page.waitForTimeout(3500);
  const box = page.locator('[contenteditable=true]').first();
  await box.click();
  await page.keyboard.type(PROMPT, { delay: 4 });
  await page.waitForTimeout(500);
  await shot(`10-${TAG}-typed`);
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
    if (i % 6 === 5) await shot(`11-${TAG}-progress-${i}`);
    if (stableFor >= 60 && t.length > 2000) break;
  }
  console.log('\nfinal url:', page.url());
  await shot(`12-${TAG}-final`);
  await shot(`13-${TAG}-final-full`, true);
  const finalText = await page.locator('body').innerText().catch(() => '');
  console.log('BODY_TAIL:\n', finalText.slice(-4000));
} else {
  console.error('usage: node intune_ui_flow.mjs connect|chat <outdir> ...');
}
await ctx.close(); await browser.close(); console.log('INTUNE_FLOW_DONE');
