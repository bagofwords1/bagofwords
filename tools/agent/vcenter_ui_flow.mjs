// Drives the BOW UI to exercise the VMware vCenter connector against the vim25
// mock (tools/vcenter/mock_vcenter.py, HTTPS with a private CA) and capture
// evidence. Verify SSL stays ON: the mock's CA PEM is pasted into the form.
//
// NOTE: @playwright/test resolves from the script's own directory (ESM), so run
// from a copy inside frontend/ (e.g. frontend/.agent-tmp/) against a stack
// booted by tools/agent/boot_stack.sh. Credentials come from env vars only.
//
//   VCENTER_CA_PEM_FILE=tools/vcenter/.certs/ca.pem \
//   node vcenter_ui_flow.mjs connect <outdir>              connect form + test connection + tables
//   node vcenter_ui_flow.mjs chat <outdir> "<prompt>" <tag> [max_wait_s]
import { chromium } from '@playwright/test';
import { mkdirSync, readFileSync } from 'node:fs';

const [MODE, OUTDIR, ...rest] = process.argv.slice(2);
const OUT = OUTDIR || '../media/pr/vcenter-connector';
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const VC_HOST = process.env.VCENTER_HOST || 'localhost';
const VC_PORT = process.env.VCENTER_PORT || '8989';
const VC_USER = process.env.VCENTER_USER || 'bow-ro@vsphere.local';
const VC_PASSWORD = process.env.VCENTER_PASSWORD || 'VMware1!RO';
const CA_PEM = process.env.VCENTER_CA_PEM_FILE ? readFileSync(process.env.VCENTER_CA_PEM_FILE, 'utf8') : '';
const DS_NAME = process.env.VCENTER_DS_NAME || 'vCenter (DC-East)';
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
// The sign-in inputs carry ids (no placeholders): #email / #password.
const emailBox = page.locator('#email, input[type=email]').first();
await emailBox.waitFor({ timeout: 30000 });
await emailBox.fill(EMAIL);
await page.locator('#password, input[type=password]').first().fill(PASSWORD);
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2500);
try { await page.getByText(/skip onboarding/i).click({ timeout: 3000 }); await page.waitForTimeout(1500); } catch {}

if (MODE === 'connect') {
  await page.goto(`${BASE}/agents/new`, { waitUntil: 'commit' });
  await page.waitForTimeout(2500);
  await shot('00-create-agent-step1');
  const portal = page.locator('#headlessui-portal-root');
  if (!(await portal.count()) || !(await portal.innerText().catch(() => '')).trim()) {
    await page.getByText(/create new connection/i).first().click({ timeout: 15000 });
  }
  await page.waitForTimeout(1500);
  try {
    const search = page.getByPlaceholder(/search/i).first();
    if (await search.count()) { await search.fill('vcenter'); await page.waitForTimeout(600); }
  } catch {}
  try { await page.getByRole('button', { name: /^infrastructure$/i }).first().click({ timeout: 4000 }); await page.waitForTimeout(600); } catch {}
  await shot('01-catalog-vcenter-tile');
  const tile = page.getByRole('button', { name: /VMware vCenter/i }).first();
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
    return false;
  };
  const nameField = page.getByPlaceholder(/name/i).first();
  if (await nameField.count()) await nameField.fill(DS_NAME).catch(() => {});
  await fill(/vcenter server/i, VC_HOST);
  await fill(/^port/i, VC_PORT);
  if (CA_PEM) {
    if (!(await fill(/ca certificate/i, CA_PEM))) {
      await page.locator('textarea').first().fill(CA_PEM).catch(() => {});
    }
  }
  await fill(/username/i, VC_USER);
  await fill(/^password/i, VC_PASSWORD);
  await shot('03-connect-form-filled');

  // The wizard's single "Connect" button runs Check connection → Schema
  // discovery in the right-hand status panel.
  const modal = page.locator('#headlessui-portal-root');
  await page.getByRole('button', { name: /^connect$/i }).first().click({ timeout: 15000 });
  for (let i = 0; i < 60; i++) {
    await page.waitForTimeout(2000);
    const t = await modal.innerText().catch(() => '');
    if (i === 2) await shot('04-test-connection');
    if (/discovered \d+ tables|\d+ tables (found|discovered)|schema discovery (complete|done)/i.test(t)) break;
    if (/failed|error/i.test(t) && i > 3) { console.log('status panel:', t.slice(0, 600)); break; }
  }
  await shot('05-schema-discovery');
  console.log('status panel:', (await modal.innerText().catch(() => '')).replace(/\n+/g, ' | ').slice(0, 500));
  for (const label of [/^connect$/i, /^continue$/i, /^done$/i, /save and continue/i, /^save$/i, /^close$/i]) {
    const b = page.getByRole('button', { name: label }).first();
    if (await b.count() && await b.isVisible().catch(() => false) && await b.isEnabled().catch(() => false)) {
      await b.click({ timeout: 8000 }).catch(() => {});
      break;
    }
  }
  await page.waitForTimeout(2500);
  console.log('url after connection save:', page.url());
  await shot('05b-step1-connection-selected');

  const agentName = page.getByPlaceholder(/sales, marketing/i).first();
  if (await agentName.count()) await agentName.fill(DS_NAME);
  await page.getByRole('button', { name: /save & continue|save and continue/i }).first().click({ timeout: 15000 }).catch(() => {});
  for (let i = 0; i < 24; i++) {
    await page.waitForTimeout(5000);
    const t = await page.locator('body').innerText().catch(() => '');
    if (/vm_snapshots|perf_counters/.test(t)) break;
  }
  console.log('url at tables step:', page.url());
  await shot('06-select-tables');
  await shot('07-select-tables-full', true);
  for (const label of [/select all/i, /all tables/i]) {
    const ctl = page.getByRole('button', { name: label }).first();
    if (await ctl.count()) { await ctl.click().catch(() => {}); break; }
    const cb = page.getByLabel(label).first();
    if (await cb.count()) { await cb.click().catch(() => {}); break; }
  }
  await page.waitForTimeout(800);
  await shot('08-select-tables-all');
  await page.getByRole('button', { name: /save & continue|save and continue|continue|next|finish/i }).first().click({ timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(4000);
  console.log('url after tables:', page.url());
  await shot('09-after-tables');
} else if (MODE === 'chat') {
  const PROMPT = rest[0] || 'prod-db-01 has been slow for the last hour. Find the root cause in vCenter.';
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
    if (stableFor >= 40 && t.length > 1500) break;
  }
  console.log('\nfinal url:', page.url());
  await shot(`12-${TAG}-final`);
  await shot(`13-${TAG}-final-full`, true);
  const finalText = await page.locator('body').innerText().catch(() => '');
  console.log('BODY_TAIL:\n', finalText.slice(-4000));
} else {
  console.error('usage: node vcenter_ui_flow.mjs connect|chat <outdir> ...');
}
await ctx.close(); await browser.close(); console.log('VCENTER_FLOW_DONE');
