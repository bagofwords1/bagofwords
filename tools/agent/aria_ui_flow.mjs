// Drives the BOW UI to exercise the VMware Aria Operations connector against
// the mock Suite API (tools/aria_operations) and capture evidence.
//
// NOTE: @playwright/test resolves from the script's own directory (ESM), so run
// from a copy inside frontend/ (e.g. frontend/.agent-tmp/) against a stack
// booted by tools/agent/boot_stack.sh. Credentials come from env vars only.
//
//   node aria_ui_flow.mjs connect <outdir>              connect form + test connection + tables
//   node aria_ui_flow.mjs chat <outdir> "<prompt>" <tag> [max_wait_s]
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const [MODE, OUTDIR, ...rest] = process.argv.slice(2);
const OUT = OUTDIR || '../media/pr/aria-operations-connector';
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const MOCK_URL = process.env.ARIA_MOCK_URL || 'http://127.0.0.1:8443';
const MOCK_USER = process.env.ARIA_MOCK_USER || 'admin';
const MOCK_PASSWORD = process.env.ARIA_MOCK_PASSWORD || 'Aria!2024';
const EXEC = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
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
await page.getByPlaceholder(/email/i).fill(EMAIL);
await page.getByPlaceholder(/password/i).fill(PASSWORD);
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2500);
try { await page.getByText(/skip onboarding/i).click({ timeout: 3000 }); await page.waitForTimeout(1500); } catch {}

if (MODE === 'connect') {
  // ── catalog grid → Aria tile → schema-generated connect form ─────────────
  await page.goto(`${BASE}/agents/new`, { waitUntil: 'commit' });
  await page.waitForTimeout(2500);
  await shot('00-create-agent-step1');
  // With no connections yet the wizard auto-opens the connector catalog modal;
  // otherwise "Create new connection" opens it.
  const portal = page.locator('#headlessui-portal-root');
  if (!(await portal.count()) || !(await portal.innerText().catch(() => '')).trim()) {
    await page.getByText(/create new connection/i).first().click({ timeout: 15000 });
  }
  await page.waitForTimeout(1500);
  // The catalog may have a search box — use it so the infra tile is on screen.
  try {
    const search = page.getByPlaceholder(/search/i).first();
    if (await search.count()) { await search.fill('aria'); await page.waitForTimeout(600); }
  } catch {}
  // Category chips filter the grid; the connector lives under Infrastructure.
  try { await page.getByRole('button', { name: /^infrastructure$/i }).first().click({ timeout: 4000 }); await page.waitForTimeout(600); } catch {}
  await shot('01-catalog-aria-tile');
  const tile = page.getByRole('button', { name: /VMware Aria Operations/i }).first();
  // The chip row is sticky inside the modal: centre the tile so it is not
  // covered, then click (force as a last resort).
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
  if (await nameField.count()) await nameField.fill('Aria Operations (prod)').catch(() => {});
  if (!(await fill(/aria operations url/i, MOCK_URL))) {
    await page.getByPlaceholder(/https:\/\/aria/i).first().fill(MOCK_URL).catch(() => {});
  }
  await fill(/username/i, MOCK_USER);
  await fill(/^password/i, MOCK_PASSWORD);
  // Verify SSL off for the plain-http mock.
  try {
    const ssl = page.getByLabel(/verify ssl/i).first();
    if (await ssl.count() && await ssl.isChecked()) await ssl.click();
  } catch {}
  await shot('03-connect-form-filled');

  const testBtn = page.getByRole('button', { name: /test connection/i }).first();
  await testBtn.click({ timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(4000);
  await shot('04-test-connection');

  // Save the connection (modal) → back on step 1 with it selected.
  await page.getByRole('button', { name: /save and continue/i }).first().click({ timeout: 15000 });
  // Schema discovery runs now; the modal reports "Discovered N tables" and
  // offers Connect.
  await page.getByText(/discovered \d+ tables/i).first().waitFor({ timeout: 120000 });
  await shot('05-schema-discovery');
  await page.getByRole('button', { name: /^connect$/i }).first().click({ timeout: 15000 });
  await page.waitForTimeout(2500);
  console.log('url after connection save:', page.url());
  await shot('05b-step1-connection-selected');

  // Name the agent and continue to Select Tables.
  const agentName = page.getByPlaceholder(/sales, marketing/i).first();
  if (await agentName.count()) await agentName.fill('Aria Operations (prod)');
  await page.getByRole('button', { name: /save & continue|save and continue/i }).first().click({ timeout: 15000 }).catch(() => {});
  // Indexing runs in the background; wait for the tables step to populate.
  for (let i = 0; i < 24; i++) {
    await page.waitForTimeout(5000);
    const t = await page.locator('body').innerText().catch(() => '');
    if (/metrics::|adapter_kinds/.test(t)) break;
  }
  console.log('url at tables step:', page.url());
  await shot('06-select-tables');
  await shot('07-select-tables-full', true);
  // Select all tables if a bulk control exists, then continue.
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
  const PROMPT = rest[0] || 'What does Aria Operations monitor? List the adapters and object counts.';
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
  console.error('usage: node aria_ui_flow.mjs connect|chat <outdir> ...');
}
await ctx.close(); await browser.close(); console.log('ARIA_FLOW_DONE');
