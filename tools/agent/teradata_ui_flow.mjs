// Drives the BOW UI to exercise the Teradata Vantage connector against the
// fake teradatasql driver (tools/teradata/README.md) and capture evidence.
//
// @playwright/test resolves from the script's own directory (ESM), so run from
// a copy inside frontend/ (e.g. frontend/.agent-tmp/) against a stack booted by
// tools/agent/boot_stack.sh. Credentials come from env vars only.
//
//   node teradata_ui_flow.mjs connect <outdir>                  tile → form → connect → save → tables
//   node teradata_ui_flow.mjs chat <outdir> "<prompt>" <tag> [max_wait_s]
//
// Env: TD_HOST (default td.example.com), TD_DATABASE, TD_USER, TD_PASSWORD,
//      TD_SSLMODE (default PREFER), PW_CHROMIUM.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const [MODE, OUTDIR, ...rest] = process.argv.slice(2);
const OUT = OUTDIR || '../media/pr/teradata-connector';
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const TD_HOST = process.env.TD_HOST || 'td.example.com';
const TD_DATABASE = process.env.TD_DATABASE || 'sales, finance, hr';
const TD_USER = process.env.TD_USER || 'bow_reader';
const TD_PASSWORD = process.env.TD_PASSWORD || '';
const TD_SSLMODE = process.env.TD_SSLMODE || 'PREFER';
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

if (MODE === 'connect') {
  await openCatalog('teradata');
  await shot('01-catalog-tile');
  const tile = page.getByRole('button', { name: /Teradata/i }).first();
  await tile.evaluate((el) => el.scrollIntoView({ block: 'center' })).catch(() => {});
  await tile.click({ timeout: 8000 }).catch(async () => { await tile.click({ force: true }); });
  await page.waitForTimeout(2000);
  await shot('02-connect-form-empty');
  await shot('02b-connect-form-empty-full', true);
  const ids = await page.locator('input, select, textarea').evaluateAll((els) => els.map((e) => `${e.tagName}#${e.id}`));
  console.log('FORM_FIELDS:', JSON.stringify(ids));

  await page.locator('#host').fill(TD_HOST);
  await page.locator('#database').fill(TD_DATABASE);
  await page.locator('#user').fill(TD_USER);
  await page.locator('#password').fill(TD_PASSWORD);
  const ssl = page.locator('#sslmode');
  if (await ssl.count()) await ssl.selectOption(TD_SSLMODE).catch(() => console.log('sslmode not a native select'));
  await shot('03-connect-form-filled');
  await shot('03b-connect-form-filled-full', true);

  const buttons = async (tag) => console.log(tag, 'BUTTONS:', JSON.stringify(await page.locator('button:visible').evaluateAll((bs) => bs.map((b) => b.innerText.trim()).filter(Boolean))));
  await page.getByRole('button', { name: /^connect$/i }).last().click({ timeout: 15000 });
  for (let i = 0; i < 48; i++) {
    const t = await page.locator('body').innerText();
    if (/discovered \d+|found \d+|failed|error/i.test(t)) break;
    await page.waitForTimeout(2500);
  }
  await page.waitForTimeout(1500);
  await shot('04-connection-check-and-discovery');
  const body = await page.locator('body').innerText();
  console.log('STATUS_PANEL:', JSON.stringify((body.match(/Connection status[\s\S]{0,600}/) || [body.slice(-800)])[0]));
  await buttons('after-connect');
  if (process.env.TD_EXPECT_FAILURE) {
    console.log('expected failure run; stopping before save');
  } else {
    for (const label of [/^continue$/i, /^done$/i, /save and continue/i, /^next$/i]) {
      const b = page.getByRole('button', { name: label }).last();
      if (await b.count() && await b.isEnabled().catch(() => false)) { await b.click().catch(() => {}); console.log('clicked', label); break; }
    }
    await page.waitForTimeout(3000);
    await shot('05-after-connection-modal');
    const agentName = page.getByPlaceholder(/sales, marketing/i).first();
    if (await agentName.count()) await agentName.fill('Teradata Sales');
    await page.getByRole('button', { name: /save & continue|save and continue/i }).first().click({ timeout: 15000 }).catch(() => {});
    for (let i = 0; i < 36; i++) {
      await page.waitForTimeout(5000);
      const t = await page.locator('body').innerText().catch(() => '');
      if (/v_monthly_revenue/.test(t)) break;
    }
    console.log('url at tables step:', page.url());
    await shot('06-select-tables');
    await shot('07-select-tables-full', true);
    const tablesText = await page.locator('body').innerText().catch(() => '');
    console.log('TABLES_STEP_HAS_HR:', /salaries/.test(tablesText), 'HAS_VIEW:', /v_monthly_revenue/.test(tablesText));
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
    await shot('08-after-tables');
  }
} else if (MODE === 'chat') {
  const PROMPT = rest[0] || 'Show revenue by region';
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
    if (stableFor >= 15 && /TERADATA_RESULT|STUB:|could not|error/i.test(t)) break;
  }
  console.log('final url:', page.url());
  await shot(`12-${TAG}-final`);
  await shot(`13-${TAG}-final-full`, true);
  const finalText = await page.locator('body').innerText().catch(() => '');
  console.log('BODY_TAIL:\n', finalText.slice(-2500));
} else {
  console.error('usage: node teradata_ui_flow.mjs connect|chat <outdir> ...');
}
const video = page.video();
await ctx.close(); await browser.close();
if (video) console.log('VIDEO:', await video.path());
console.log('TERADATA_FLOW_DONE');
