// Live check for two report-page fixes, against a booted stack with an LLM
// (the deterministic stub works: it only has to emit one create_data with
// long category names, and one read_file on an uploaded image).
//
//   1. Inline chart with long category names keeps a usable plot height
//      (rotated x-axis labels are truncated instead of eating the card).
//   2. Closing the data / file tab that opened the side panel closes the
//      panel — it no longer lands on an empty Dashboard tab. When the panel
//      was already open, closing the tab returns to the view underneath.
//
//   cd frontend && PW_CHROMIUM_PATH=/opt/pw-browsers/chromium \
//     node tests/reports/shoot-panel-close-and-chart-labels.mjs <outdir> <image.png>
//
// Exits non-zero when an expectation fails, so it doubles as a smoke test.
import { chromium } from '@playwright/test';
import { mkdirSync, readFileSync } from 'node:fs';
import { basename } from 'node:path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000';
const API = process.env.BOW_BASE_URL || 'http://localhost:8000';
const EMAIL = process.env.BOW_ADMIN_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_ADMIN_PASSWORD || 'Password123!';
const OUT = process.argv[2] || '../media/pr/panel-close';
const IMAGE = process.argv[3];
const STRICT = process.env.STRICT !== '0';

mkdirSync(OUT, { recursive: true });
const failures = [];
async function shot(target, name) {
  const path = `${OUT}/${name}.png`;
  await target.screenshot({ path });
  console.log('captured', path);
}
function check(cond, msg) {
  console.log(cond ? 'ok  -' : 'FAIL-', msg);
  if (!cond) failures.push(msg);
}

// ---- API setup -------------------------------------------------------------
const form = new URLSearchParams({ username: EMAIL, password: PASSWORD });
const tok = (await (await fetch(`${API}/api/auth/jwt/login`, { method: 'POST', body: form })).json()).access_token;
const orgs = await (await fetch(`${API}/api/organizations`, { headers: { Authorization: `Bearer ${tok}` } })).json();
const AUTH = { Authorization: `Bearer ${tok}`, 'X-Organization-Id': orgs[0].id };
const H = { ...AUTH, 'Content-Type': 'application/json' };
const dss = await (await fetch(`${API}/api/data_sources`, { headers: H })).json();
async function newReport(title, withSource = true) {
  return (await fetch(`${API}/api/reports`, {
    method: 'POST', headers: H,
    body: JSON.stringify({ title, data_sources: withSource ? [dss[0].id] : [] }),
  })).json();
}

const exe = process.env.PW_CHROMIUM_PATH;
const browser = await chromium.launch(exe ? { executablePath: exe } : {});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

// The side panel's tab strip is only rendered while the split is open.
const summaryTab = () => page.getByRole('button', { name: 'Summary', exact: true });
const panelOpen = () => summaryTab().isVisible();

async function send(prompt) {
  const editor = page.locator('.mention-input-field').first();
  await editor.waitFor({ state: 'visible', timeout: 180000 });
  await editor.click();
  await page.keyboard.type(prompt);
  await page.waitForTimeout(800);
  await page.locator('button.w-7.h-7.rounded-full:not([disabled])').last().click();
  await page.getByTestId('stop-button').waitFor({ state: 'visible', timeout: 30000 }).catch(() => {});
}

try {
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'load' });
  await page.locator('#email').waitFor({ state: 'visible', timeout: 180000 });
  await page.fill('#email', EMAIL);
  await page.fill('#password', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 180000 });

  // ---- 1. inline chart height ---------------------------------------------
  const r1 = await newReport('Q3 Cloud Costs by Region/Service');
  await page.goto(`${BASE}/reports/${r1.id}`, { waitUntil: 'load' });
  await send('Q3 cloud costs by region and service');
  const expandBtn = page.getByTestId('widget-open-panel').first();
  await expandBtn.waitFor({ state: 'visible', timeout: 240000 });
  await page.getByTestId('stop-button').waitFor({ state: 'hidden', timeout: 240000 });
  const card = page.locator('.widget-container').first();
  await card.scrollIntoViewIfNeeded();
  await page.waitForTimeout(2000);
  await shot(card, '01-inline-chart');

  // ---- 2a. data tab opened the panel → closing it closes the panel --------
  check(!(await panelOpen()), 'side panel starts closed');
  await expandBtn.click();
  await page.getByTestId('data-panel').waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForTimeout(1500);
  await shot(page, '02-data-panel-open');
  await page.getByTestId('data-panel-close').click();
  await page.waitForTimeout(1200);
  await shot(page, '03-data-panel-closed');
  check(!(await panelOpen()), 'closing the data tab that opened the panel closes the panel');
  check(!(await page.getByText('Ready to create a dashboard').isVisible()), 'no empty dashboard shown after close');

  // ---- 2b. panel already open → closing returns to the previous view ------
  // Header split toggle — labelled "Sidebar" while the split is closed.
  if (!(await panelOpen())) await page.getByRole('button', { name: 'Sidebar', exact: true }).click();
  await summaryTab().waitFor({ state: 'visible', timeout: 10000 });
  await summaryTab().click();
  await page.waitForTimeout(800);
  await expandBtn.click();
  await page.getByTestId('data-panel').waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForTimeout(800);
  await page.getByTestId('data-panel-close').click();
  await page.waitForTimeout(1200);
  await shot(page, '04-data-close-returns-to-summary');
  check(await panelOpen(), 'panel stays open when it was already open before');
  check((await summaryTab().getAttribute('class')).includes('bg-gray-100'), 'returns to the Summary tab');

  // ---- 3. file tab opened the panel → closing it closes the panel ---------
  if (IMAGE) {
    // Close the split first so the file is what opens it.
    if (await panelOpen()) await page.getByRole('button', { name: 'Share' }).first().locator('xpath=following::button[1]').click();
    const r2 = await newReport('Architecture diagram', false);
    const fd = new FormData();
    fd.append('file', new Blob([readFileSync(IMAGE)], { type: 'image/png' }), basename(IMAGE));
    fd.append('report_id', r2.id);
    const up = await (await fetch(`${API}/api/files`, { method: 'POST', headers: AUTH, body: fd })).json();
    check(!!up.id, `image uploaded (${up.id})`);
    await page.goto(`${BASE}/reports/${r2.id}`, { waitUntil: 'load' });
    await send(`READFILE ${up.id}`);
    const fileExpand = page.getByRole('button', { name: 'Open in side panel' }).first();
    await fileExpand.waitFor({ state: 'visible', timeout: 240000 });
    await page.getByTestId('stop-button').waitFor({ state: 'hidden', timeout: 240000 }).catch(() => {});
    await page.waitForTimeout(1000);
    check(!(await panelOpen()), 'side panel starts closed (file)');
    await fileExpand.click();
    await page.getByRole('button', { name: /Close file/ }).or(page.locator('[aria-label="Close file"]')).first()
      .waitFor({ state: 'visible', timeout: 15000 });
    await page.waitForTimeout(1500);
    await shot(page, '05-file-panel-open');
    await page.locator('[aria-label="Close file"]').first().click();
    await page.waitForTimeout(1200);
    await shot(page, '06-file-panel-closed');
    check(!(await panelOpen()), 'closing the file tab that opened the panel closes the panel');
  }
} catch (e) {
  await shot(page, '99-failure').catch(() => {});
  console.error(e);
  failures.push(String(e));
} finally {
  await browser.close();
}
console.log(failures.length ? `\n${failures.length} FAILED` : '\nALL OK');
if (STRICT && failures.length) process.exitCode = 1;
