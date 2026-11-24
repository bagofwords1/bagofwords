// Drives the BOW UI to exercise the SharePoint Server lists connector
// (sharepoint_onprem_lists) against mock_sharepoint_lists_server.py (real NTLM)
// and capture evidence. See docs/feedback-loops/sharepoint-onprem-lists-connector.md.
// @playwright/test resolves from the script's own directory (ESM), so run from
// a copy inside frontend/.
//
//   node sharepoint_lists_ui_flow.mjs connect <outdir> [site_url] [username] [password]
//   node sharepoint_lists_ui_flow.mjs chat <outdir> "<prompt>" <tag> [max_wait_s]
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const [MODE, OUTDIR, ...rest] = process.argv.slice(2);
const OUT = OUTDIR || '../media/pr/sharepoint-onprem-lists';
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const SITE_URL = rest[0] || process.env.SP_SITE_URL || 'https://localhost:8444/sites/ops';
const SP_USER = rest[1] || process.env.SP_USERNAME || 'CORP\\svc-sharepoint';
const SP_PASSWORD = rest[2] || process.env.SP_PASSWORD || '';
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
    if (await search.count()) { await search.fill('sharepoint'); await page.waitForTimeout(600); }
  } catch {}
  await shot('01-catalog-sharepoint-tiles');
  const tile = page.getByRole('button', { name: /SharePoint Server Lists/i }).first();
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
  if (await nameField.count()) await nameField.fill('Ops lists').catch(() => {});
  await fill(/site url/i, SITE_URL);
  await fill(/domain username/i, SP_USER);
  await fill(/^password/i, SP_PASSWORD);
  await shot('03-connect-form-filled');

  // "Connect" runs the connection check, then schema discovery, in the
  // status panel on the right.
  await page.getByRole('button', { name: /^connect$/i }).first().click({ timeout: 15000 });
  let status = '';
  for (let i = 0; i < 60; i++) {
    await page.waitForTimeout(2000);
    status = await page.locator('body').innerText().catch(() => '');
    if (/discovered \d+ (tables|lists)|Connected to SharePoint|HTTP 401|failed|error/i.test(status) && i > 1) break;
  }
  await shot('04-connection-status');
  const m = status.match(/(Connected to SharePoint[^\n]*|[^\n]*HTTP 401[^\n]*)/);
  console.log('TEST_RESULT:', m ? m[1] : '(no message found)');
  const d = status.match(/discovered \d+ (tables|lists)/i);
  console.log('DISCOVERY:', d ? d[0] : '(none)');
  if (process.env.STOP_AFTER_TEST || !d) { console.log('STATUS_PANEL:', status.slice(status.indexOf('Connection status'), status.indexOf('Connection status') + 600)); await ctx.close(); await browser.close(); process.exit(0); }
  await shot('05-schema-discovery');
  await page.getByRole('button', { name: /^(done|connect)$/i }).first().click({ timeout: 15000 });
  await page.waitForTimeout(2500);
  const agentName = page.getByPlaceholder(/sales, marketing/i).first();
  if (await agentName.count()) await agentName.fill('Ops lists');
  await page.getByRole('button', { name: /save & continue|save and continue/i }).first().click({ timeout: 15000 }).catch(() => {});
  for (let i = 0; i < 24; i++) {
    await page.waitForTimeout(5000);
    const t = await page.locator('body').innerText().catch(() => '');
    if (/Tickets/.test(t) && /Expenses/.test(t)) break;
  }
  console.log('url at tables step:', page.url());
  await shot('06-select-tables');
  const tablesTxt = await page.locator('body').innerText().catch(() => '');
  console.log('TABLES_SEEN:', ['Expenses','Tickets','Projects','Rota','Salaries'].filter((t) => tablesTxt.includes(t)).join(','));
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
  const PROMPT = rest[0] || 'How many open tickets with priority 4 or higher does each person have?';
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
  console.error('usage: node sharepoint_lists_ui_flow.mjs connect|chat <outdir> ...');
}
await ctx.close(); await browser.close(); console.log('SHAREPOINT_LISTS_FLOW_DONE');
