// Live walkthrough of the query-result side pane (ToolWidgetPreview in
// `expanded` mode) against a booted stack with a real LLM configured.
//
//   tools/agent/boot_stack.sh --dev
//   cd backend && uv run python ../tools/agent/seed_org.py --demo
//   cd backend && ANTHROPIC_API_KEY=... uv run python ../tools/agent/setup_haiku_llm.py
//   cd frontend && PW_CHROMIUM_PATH=/opt/pw-browsers/chromium \
//     node tests/reports/shoot-data-side-pane.mjs ../media/pr/toolwidget-preview-side-pane
//
// It asks the agent for a bar chart on the demo Music Store, then opens the
// create_data result in the side pane and captures every tab plus the edit
// modal. Exits non-zero when any expectation fails, so it doubles as a
// smoke test for the feature.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000';
const API = process.env.BOW_BASE_URL || 'http://localhost:8000';
const EMAIL = process.env.BOW_ADMIN_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_ADMIN_PASSWORD || 'Password123!';
const OUT = process.argv[2] || '../media/pr/toolwidget-preview-side-pane';
const PROMPT = process.argv[3] || 'Show total invoice revenue by genre as a bar chart, sorted descending';

mkdirSync(OUT, { recursive: true });
const shots = [];
async function shot(page, name) {
  const path = `${OUT}/${name}.png`;
  await page.screenshot({ path });
  shots.push(path);
  console.log('captured', path);
}
function check(cond, msg) {
  if (!cond) throw new Error(`EXPECTATION FAILED: ${msg}`);
  console.log('ok -', msg);
}

// ---- report through the API (same org/data source the seed created) -----
const form = new URLSearchParams({ username: EMAIL, password: PASSWORD });
const tok = (await (await fetch(`${API}/api/auth/jwt/login`, { method: 'POST', body: form })).json()).access_token;
const orgs = await (await fetch(`${API}/api/organizations`, { headers: { Authorization: `Bearer ${tok}` } })).json();
const H = { Authorization: `Bearer ${tok}`, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' };
const dss = await (await fetch(`${API}/api/data_sources`, { headers: H })).json();
check(dss.length > 0, 'a data source is installed');
const report = await (await fetch(`${API}/api/reports`, {
  method: 'POST', headers: H,
  body: JSON.stringify({ title: 'Side pane walkthrough', data_sources: [dss[0].id] }),
})).json();
check(report?.id, `report created (${report?.id})`);

// ---- browser -------------------------------------------------------------
// Cloud sandboxes ship Chromium at /opt/pw-browsers/chromium; when the pinned
// playwright build differs from it, point the launcher at that binary.
const exe = process.env.PW_CHROMIUM_PATH;
const browser = await chromium.launch(exe ? { executablePath: exe } : {});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
const consoleErrors = [];
page.on('pageerror', (e) => consoleErrors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });

try {
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'load' });
  await page.fill('#email', EMAIL);
  await page.fill('#password', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 });

  await page.goto(`${BASE}/reports/${report.id}`, { waitUntil: 'load' });
  const editor = page.locator('.mention-input-field').first();
  // Dev mode compiles the report page on first request — allow for it.
  await editor.waitFor({ state: 'visible', timeout: 180000 });
  // The walkthrough is only meaningful on the model the org defaulted to.
  await page.getByText(/Haiku/).first().waitFor({ state: 'visible', timeout: 60000 });
  check(true, 'prompt box shows the Haiku default model');
  await editor.click();
  await page.keyboard.type(PROMPT);
  await page.keyboard.press('Enter');

  // The card's expand affordance shows up as soon as a step exists; wait for
  // the run to actually finish so the chart is settled before shooting.
  const expandBtn = page.getByTestId('widget-open-panel').first();
  await expandBtn.waitFor({ state: 'visible', timeout: 240000 });
  await page.getByTestId('stop-button').waitFor({ state: 'hidden', timeout: 240000 });
  await expandBtn.scrollIntoViewIfNeeded();
  await page.waitForTimeout(1500);
  await shot(page, '01-inline-card');

  // ---- open in the side pane ----------------------------------------------
  await expandBtn.click();
  const panel = page.getByTestId('data-panel');
  await panel.waitFor({ state: 'visible', timeout: 15000 });
  const tab = page.getByTestId('data-panel-tab');
  await tab.waitFor({ state: 'visible' });
  check(await tab.getAttribute('class').then((c) => c.includes('bg-gray-100')), 'data tab is the active right-pane tab');
  await page.waitForTimeout(1500);

  // The pane mounts on the inline preview and swaps in the full step; wait
  // for that swap so the shot shows the settled result.
  const cardRows = (await page.locator('.widget-container').first().getByTestId('widget-row-count').textContent()).trim();
  await panel.getByTestId('widget-row-count').filter({ hasText: cardRows }).waitFor({ state: 'visible', timeout: 20000 });
  await panel.locator('.animate-spin, svg.animate-spin').first().waitFor({ state: 'hidden', timeout: 20000 }).catch(() => {});
  check(true, `pane hydrated to the full result (${cardRows})`);
  const box = await panel.boundingBox();
  const vp = page.viewportSize();
  check(box && box.width > vp.width * 0.45, `pane is wide (${Math.round(box?.width || 0)}px of ${vp.width})`);
  check(box && box.height > vp.height * 0.8, `pane is tall (${Math.round(box?.height || 0)}px of ${vp.height})`);
  const canvas = panel.locator('canvas').first();
  const cbox = await canvas.boundingBox();
  check(cbox && cbox.height > 400, `chart canvas fills the pane (${Math.round(cbox?.height || 0)}px tall)`);
  await shot(page, '02-pane-chart');

  // Controls that must be present in the pane, exactly like the card.
  const editBtn = panel.getByRole('button', { name: /^Edit$/ }).first();
  check(await editBtn.isVisible(), 'Edit button present in the pane header');
  check(await panel.getByRole('button', { name: 'Add to Dashboard' }).isVisible(), 'Add to Dashboard present in the pane');
  check(await panel.getByRole('button', { name: 'Save Query' }).isVisible(), 'Save Query present in the pane');
  check(await panel.getByTestId('widget-open-panel').count() === 0, 'no nested "open in panel" button inside the pane');

  // ---- Data tab -----------------------------------------------------------
  await panel.getByRole('button', { name: 'Data', exact: true }).click();
  const grid = panel.locator('.ag-root').first();
  await grid.waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForTimeout(800);
  const gbox = await grid.boundingBox();
  check(gbox && gbox.height > 400, `grid fills the pane (${Math.round(gbox?.height || 0)}px tall)`);
  await shot(page, '03-pane-data');

  // ---- Code tab -----------------------------------------------------------
  await panel.getByRole('button', { name: 'Code', exact: true }).click();
  await panel.locator('.monaco-editor').first().waitFor({ state: 'visible', timeout: 20000 });
  await page.waitForTimeout(800);
  await shot(page, '04-pane-code');

  // ---- Edit opens the query editor modal ----------------------------------
  await editBtn.click();
  const modal = page.getByText(/^Edit query/).first();
  await modal.waitFor({ state: 'visible', timeout: 20000 });
  await page.waitForTimeout(1200);
  await shot(page, '05-pane-edit-modal');
  await page.getByRole('button', { name: 'Close', exact: true }).first().click();
  await modal.waitFor({ state: 'hidden', timeout: 10000 });

  // ---- Add to Dashboard from the pane ------------------------------------
  // The app switches the pane to the new dashboard on success; the data tab
  // must survive that and show the "Added" state when reopened.
  await panel.getByRole('button', { name: 'Chart', exact: true }).click();
  await panel.getByRole('button', { name: 'Add to Dashboard' }).click();
  await page.getByRole('button', { name: 'Dashboard', exact: true }).waitFor({ state: 'visible', timeout: 5000 });
  await page.locator('.gridstack-item, .grid-stack-item, iframe').first().waitFor({ state: 'visible', timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(2500);
  check(await tab.isVisible(), 'data tab remains while the dashboard is shown');
  await shot(page, '06-added-to-dashboard');
  await tab.click();
  await panel.waitFor({ state: 'visible', timeout: 10000 });
  await panel.getByText('Added to Dashboard').waitFor({ state: 'visible', timeout: 10000 });
  await panel.getByTestId('widget-row-count').filter({ hasText: cardRows }).waitFor({ state: 'visible', timeout: 20000 });
  check(true, 'data tab re-opens the pane, now marked "Added to Dashboard"');
  await page.waitForTimeout(800);
  await shot(page, '07-pane-after-add');

  // ---- close ---------------------------------------------------------------
  await page.getByTestId('data-panel-close').click();
  await tab.waitFor({ state: 'hidden', timeout: 5000 });
  check((await panel.count()) === 0, 'closing the tab unmounts the pane');
  await page.waitForTimeout(600);
  await shot(page, '08-after-close');

  // whoami 401 is the session probe on the unauthenticated sign-in page.
  const realErrors = consoleErrors.filter((e) => !/favicon|net::ERR_|ResizeObserver|Failed to load resource|whoami.*401/.test(e));
  check(realErrors.length === 0, `no page errors (${realErrors.length})${realErrors.length ? ': ' + realErrors.slice(0, 3).join(' | ') : ''}`);
  console.log(JSON.stringify({ report_id: report.id, shots }, null, 2));
} catch (e) {
  await shot(page, '99-failure').catch(() => {});
  console.error(e);
  if (consoleErrors.length) console.error('console errors:', consoleErrors.slice(0, 10));
  process.exitCode = 1;
} finally {
  await browser.close();
}
