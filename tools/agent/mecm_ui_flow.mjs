// Drives the BOW UI to exercise the Configuration Manager (SCCM / MECM)
// connector against the mock AdminService (tools/mecm/mock_adminservice.py)
// and capture evidence.
//
// @playwright/test resolves from the script's own directory (ESM), so run from
// a copy inside frontend/ (e.g. frontend/.agent-tmp/) against a stack booted by
// tools/agent/boot_stack.sh. Credentials come from env vars only.
//
//   node mecm_ui_flow.mjs catalog <outdir> <tag>                catalog search for the tile
//   node mecm_ui_flow.mjs connect <outdir>                      tile → form → test → save → classes
//   node mecm_ui_flow.mjs chat <outdir> "<prompt>" <tag> [max_wait_s]
//
// Env: MECM_URL (default localhost:8443), MECM_USER, MECM_PASSWORD,
//      MECM_CA_FILE (PEM of the mock's self-signed certificate), PW_CHROMIUM.
import { chromium } from '@playwright/test';
import { mkdirSync, readFileSync } from 'node:fs';

const [MODE, OUTDIR, ...rest] = process.argv.slice(2);
const OUT = OUTDIR || '../media/pr/mecm-connector';
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const MECM_URL = process.env.MECM_URL || 'localhost:8443';
const MECM_USER = process.env.MECM_USER || 'CONTOSO\\svc_bow';
const MECM_PASSWORD = process.env.MECM_PASSWORD || 'Passw0rd!';
const CA_PEM = process.env.MECM_CA_FILE ? readFileSync(process.env.MECM_CA_FILE, 'utf8') : '';
const EXEC = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1243/chrome-linux64/chrome';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({ executablePath: EXEC });
// RECORD_DIR=<dir> records a .webm of the run (convert to GIF with ffmpeg for the PR).
const ctx = await browser.newContext({
  viewport: { width: 1512, height: 1000 },
  ...(process.env.RECORD_DIR ? { recordVideo: { dir: process.env.RECORD_DIR, size: { width: 1512, height: 1000 } } } : {}),
});
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (name, full = false) => {
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: full });
  console.log('shot', name);
};

// ── sign in ────────────────────────────────────────────────────────────────
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'commit' });
await page.locator('input#email, input[type=email]').first().fill(EMAIL);
await page.locator('input#password, input[type=password]').first().fill(PASSWORD);
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2500);
try { await page.getByText(/skip onboarding/i).click({ timeout: 3000 }); await page.waitForTimeout(1500); } catch {}

async function openCatalog(query) {
  await page.goto(`${BASE}/agents/new`, { waitUntil: 'commit' });
  await page.waitForTimeout(3000);
  const portal = page.locator('#headlessui-portal-root');
  if (!(await portal.count()) || !(await portal.innerText().catch(() => '')).trim()) {
    await page.getByText(/create new connection/i).first().click({ timeout: 15000 });
  }
  await page.waitForTimeout(1500);
  const search = page.getByPlaceholder(/search/i).first();
  if (await search.count()) { await search.fill(query); await page.waitForTimeout(800); }
}

if (MODE === 'catalog') {
  const TAG = rest[0] || 'catalog';
  await openCatalog('configuration manager');
  await shot(`${TAG}-search-configuration-manager`);
  await openCatalog('sccm');
  await shot(`${TAG}-search-sccm`);
} else if (MODE === 'connect') {
  await openCatalog('sccm');
  await shot('01-catalog-tile');
  const tile = page.getByRole('button', { name: /Configuration Manager/i }).first();
  await tile.evaluate((el) => el.scrollIntoView({ block: 'center' })).catch(() => {});
  await tile.click({ timeout: 8000 }).catch(async () => { await tile.click({ force: true }); });
  await page.waitForTimeout(2000);
  await shot('02-connect-form-empty');
  await shot('02b-connect-form-empty-full', true);

  // The name field is prefilled with the connector title; names are unique per org.
  const CONN_NAME = process.env.MECM_CONN_NAME || 'Contoso SCCM (PS1)';
  const idx = await page.locator('input').evaluateAll((els) => els.findIndex((e) => e.value === 'Microsoft Configuration Manager (SCCM)'));
  if (idx >= 0) await page.locator('input').nth(idx).fill(CONN_NAME);
  await page.locator('#server_url').fill(MECM_URL);
  await page.locator('#username').fill(MECM_USER);
  await page.locator('#password').fill(MECM_PASSWORD);
  if (CA_PEM) await page.locator('#ca_certificate').fill(CA_PEM);
  await shot('03-connect-form-filled');

  const buttons = async (tag) => console.log(tag, 'BUTTONS:', JSON.stringify(await page.locator('button:visible').evaluateAll((bs) => bs.map((b) => b.innerText.trim()).filter(Boolean))));
  // "Connect" runs the connection check + schema discovery in the status panel.
  await page.getByRole('button', { name: /^connect$/i }).last().click({ timeout: 15000 });
  await page.getByText(/connected to configuration manager site|discovered \d+|failed|error/i).first().waitFor({ timeout: 180000 }).catch(() => {});
  for (let i = 0; i < 36; i++) {
    const t = await page.locator('body').innerText();
    if (/discovered \d+|found \d+ class/i.test(t) || /failed|error/i.test(t)) break;
    await page.waitForTimeout(2500);
  }
  await page.waitForTimeout(1500);
  await shot('04-connection-check-and-discovery');
  const body = await page.locator('body').innerText();
  console.log('STATUS_PANEL:', JSON.stringify((body.match(/Connection status[\s\S]{0,600}/) || ['?'])[0]));
  await buttons('after-connect');
  // Continue out of the modal (button label varies by wizard step).
  for (const label of [/^continue$/i, /^done$/i, /save and continue/i, /^connect$/i, /^next$/i]) {
    const b = page.getByRole('button', { name: label }).last();
    if (await b.count() && await b.isEnabled().catch(() => false)) { await b.click().catch(() => {}); console.log('clicked', label); break; }
  }
  await page.waitForTimeout(3000);
  await shot('05-after-connection-modal');
  await buttons('after-modal');

  const agentName = page.getByPlaceholder(/sales, marketing/i).first();
  if (await agentName.count()) await agentName.fill('Contoso SCCM');
  await page.getByRole('button', { name: /save & continue|save and continue/i }).first().click({ timeout: 15000 }).catch(() => {});
  for (let i = 0; i < 36; i++) {
    await page.waitForTimeout(5000);
    const t = await page.locator('body').innerText().catch(() => '');
    if (/SMS_R_System/.test(t)) break;
  }
  console.log('url at tables step:', page.url());
  await shot('06-select-classes');
  await shot('07-select-classes-full', true);
  for (const label of [/select all/i, /all tables/i, /all classes/i]) {
    const ctl = page.getByRole('button', { name: label }).first();
    if (await ctl.count()) { await ctl.click().catch(() => {}); break; }
    const cb = page.getByLabel(label).first();
    if (await cb.count()) { await cb.click().catch(() => {}); break; }
  }
  await page.waitForTimeout(800);
  await shot('08-select-classes-all');
  await page.getByRole('button', { name: /save & continue|save and continue|continue|next|finish/i }).first().click({ timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(4000);
  console.log('url after classes:', page.url());
  await shot('09-after-classes');
} else if (MODE === 'chat') {
  const PROMPT = rest[0] || 'How many devices run each operating system?';
  const TAG = rest[1] || 'chat';
  const WAIT = parseInt(rest[2] || '300', 10);
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
  for (let i = 0; i < Math.ceil(WAIT / 5); i++) {
    await page.waitForTimeout(5000);
    const t = await page.locator('body').innerText().catch(() => '');
    if (t.length === lastLen) stableFor += 5; else { stableFor = 0; lastLen = t.length; }
    if (stableFor >= 20 && /Configuration Manager AdminService data|could not|error/i.test(t)) break;
  }
  console.log('final url:', page.url());
  await shot(`12-${TAG}-final`);
  await shot(`13-${TAG}-final-full`, true);
  const finalText = await page.locator('body').innerText().catch(() => '');
  console.log('BODY_TAIL:\n', finalText.slice(-2500));
} else {
  console.error('usage: node mecm_ui_flow.mjs catalog|connect|chat <outdir> ...');
}
const video = page.video();
await ctx.close(); await browser.close();
if (video) console.log('VIDEO:', await video.path());
console.log('MECM_FLOW_DONE');
