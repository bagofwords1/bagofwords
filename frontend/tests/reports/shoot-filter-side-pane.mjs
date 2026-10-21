// Live check that the result filter narrows the table in the side pane.
//
// The same result renders twice (chat card + side pane); before the fix in
// components/AgGridComponent.vue only one of the two grids picked up the
// filtered rows, so the pane header read "4/34 rows" while the grid still
// showed all 34.
//
//   tools/agent/boot_stack.sh --dev
//   cd backend && uv run python ../tools/agent/seed_org.py --demo
//   cd backend && ANTHROPIC_API_KEY=... uv run python ../tools/agent/setup_haiku_llm.py
//   cd frontend && PW_CHROMIUM_PATH=/opt/pw-browsers/chromium \
//     node tests/reports/shoot-filter-side-pane.mjs <out.png> [reportId]
//
// Without a reportId it creates a report and asks the agent for a table;
// with one it reuses that report (so before/after shots show the same data).
// Prints the report id, and exits non-zero when the pane grid's row count
// doesn't match the filtered count in the header.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000';
const API = process.env.BOW_BASE_URL || 'http://localhost:8000';
const EMAIL = process.env.BOW_ADMIN_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_ADMIN_PASSWORD || 'Password123!';
const OUT = process.argv[2] || '../media/pr/filter-side-pane/after.png';
let reportId = process.argv[3];
const PROMPT = 'Show total invoice revenue by month as a table (columns: month, total_revenue), sorted by month. Table only, no chart.';

mkdirSync(dirname(OUT), { recursive: true });

if (!reportId) {
  const form = new URLSearchParams({ username: EMAIL, password: PASSWORD });
  const tok = (await (await fetch(`${API}/api/auth/jwt/login`, { method: 'POST', body: form })).json()).access_token;
  const orgs = await (await fetch(`${API}/api/organizations`, { headers: { Authorization: `Bearer ${tok}` } })).json();
  const H = { Authorization: `Bearer ${tok}`, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' };
  const dss = await (await fetch(`${API}/api/data_sources`, { headers: H })).json();
  const report = await (await fetch(`${API}/api/reports`, {
    method: 'POST', headers: H,
    body: JSON.stringify({ title: 'Filter in side pane', data_sources: [dss[0].id] }),
  })).json();
  reportId = report.id;
}
console.log('report', reportId);

const exe = process.env.PW_CHROMIUM_PATH;
const browser = await chromium.launch(exe ? { executablePath: exe } : {});
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();

let failed = false;
try {
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'load' });
  await page.fill('#email', EMAIL);
  await page.fill('#password', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 });

  await page.goto(`${BASE}/reports/${reportId}`, { waitUntil: 'load' });
  const editor = page.locator('.mention-input-field').first();
  await editor.waitFor({ state: 'visible', timeout: 180000 });
  const expandBtn = page.getByTestId('widget-open-panel').first();
  if (process.argv[3] === undefined) {
    await editor.click();
    await page.keyboard.type(PROMPT);
    await page.keyboard.press('Enter');
    await expandBtn.waitFor({ state: 'visible', timeout: 240000 });
    await page.getByTestId('stop-button').waitFor({ state: 'hidden', timeout: 240000 });
  }
  await expandBtn.waitFor({ state: 'visible', timeout: 60000 });
  await page.waitForTimeout(1500);

  // Open the result in the side pane — now it renders in two grids.
  await expandBtn.click();
  const panel = page.getByTestId('data-panel');
  await panel.waitFor({ state: 'visible', timeout: 15000 });
  await panel.locator('.animate-spin').first().waitFor({ state: 'hidden', timeout: 20000 }).catch(() => {});
  const dataTab = panel.getByRole('button', { name: 'Data', exact: true });
  if (await dataTab.count()) await dataTab.click();
  const grid = panel.locator('.ag-root').first();
  await grid.waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForTimeout(1000);

  // Pick the numeric column and a threshold at its median.
  const header = (await panel.getByTestId('widget-row-count').textContent()).trim();
  const total = Number(header.replace(/[^0-9]/g, ''));
  const colId = await grid.evaluate((el) => {
    const cells = [...el.querySelectorAll('.ag-center-cols-container .ag-row[row-index="0"] .ag-cell')];
    const c = cells.find((x) => x.textContent.trim() !== '' && !isNaN(Number(x.textContent.trim())) && !/^\d{4}/.test(x.textContent.trim()));
    return c?.getAttribute('col-id');
  });
  const values = await grid.evaluate((el, id) =>
    [...el.querySelectorAll(`.ag-center-cols-container .ag-cell[col-id="${id}"]`)].map((c) => Number(c.textContent.trim())), colId);
  values.sort((a, b) => a - b);
  const threshold = values[Math.floor(values.length / 2)];
  console.log(`numeric column ${colId}, ${total} rows, threshold > ${threshold}`);

  // Drive the filter popover like a user: Add filter → column → ">" → value → Apply.
  await panel.locator('div.justify-end.mb-2 button').first().click();
  await page.getByRole('button', { name: 'Add filter' }).click();
  const selects = page.locator('[role="dialog"] button[role="combobox"], [id^="headlessui-popover-panel"] button[aria-haspopup]');
  await selects.nth(0).click();
  await page.keyboard.type(colId.replace(/_/g, ' ').split(' ')[1] || colId);
  await page.keyboard.press('Enter');
  await selects.nth(1).click();
  await page.getByRole('option', { name: '>', exact: true }).click();
  await page.locator('[id^="headlessui-popover-panel"] input[type="number"]').fill(String(threshold));
  await page.waitForTimeout(300);
  await page.getByRole('button', { name: 'Apply', exact: true }).click();
  await page.waitForTimeout(1500);

  const headerAfter = (await panel.getByTestId('widget-row-count').textContent()).trim();
  const expected = Number(headerAfter.split('/')[0].replace(/[^0-9]/g, ''));
  const shown = await grid.evaluate((el) => el.querySelectorAll('.ag-center-cols-container .ag-row').length);
  const tooLow = await grid.evaluate((el, [id, t]) =>
    [...el.querySelectorAll(`.ag-center-cols-container .ag-cell[col-id="${id}"]`)].filter((c) => Number(c.textContent.trim()) <= t).length,
  [colId, threshold]);
  console.log(`pane header "${headerAfter}", grid shows ${shown} rows, ${tooLow} of them fail the filter`);
  failed = shown !== expected || tooLow > 0;

  // Reopen the popover so the shot shows the applied filter next to the grid.
  await panel.locator('div.justify-end.mb-2 button').first().click();
  await page.waitForTimeout(600);
  await page.screenshot({ path: OUT });
  console.log('captured', OUT);
} finally {
  await browser.close();
}
console.log(failed ? 'FAIL: pane grid ignores the filter' : 'PASS: pane grid matches the filter');
process.exit(failed ? 1 : 0);
