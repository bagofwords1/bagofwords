// UI loop for per-agent PII policies (docs/feedback-loops/pii-agent-policies.md).
//
//   node tools/agent/pii_agent_policies_ui.mjs shots <outdir> <prefix>
//       screenshots of Settings → PII and an agent's settings panel
//   node tools/agent/pii_agent_policies_ui.mjs flow <outdir>
//       sets the policies through both UIs and checks they persist:
//         Settings → PII: default agent policy = "Sales (emails)", HR = "HR (emails + phones)"
//         Agent settings (Sales): back to the org default, then "HR (emails + phones)"
//
// Env: BOW_EMAIL / BOW_PASSWORD (admin), HR_AGENT / SALES_AGENT (agent ids),
//      BOW_UI (default http://localhost:3000), BOW_LOCALE (e.g. he).
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

const requireFromFrontend = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = requireFromFrontend('@playwright/test');

const [mode, out, prefix = 'after'] = process.argv.slice(2);
const origin = process.env.BOW_UI || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const locale = process.env.BOW_LOCALE || '';
const HR = process.env.HR_AGENT;
const SALES = process.env.SALES_AGENT;
if (!mode || !out || !HR || !SALES) {
  console.error('usage: HR_AGENT=.. SALES_AGENT=.. node pii_agent_policies_ui.mjs shots|flow <outdir> [prefix]');
  process.exit(2);
}
mkdirSync(out, { recursive: true });

// In cloud sandboxes whose Playwright version differs from the project's, point
// PW_CHROMIUM at the pre-installed browser (e.g. /opt/pw-browsers/chromium).
const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`);
};
const suffix = locale ? `-${locale}` : '';
const shot = (name, opts = {}) => page.screenshot({ path: join(out, `${name}${suffix}.png`), ...opts });

await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.locator('#email').waitFor({ timeout: 120000 });
await page.fill('#email', email);
await page.fill('#password', password);
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.click('button[type=submit]'),
]);
await page.waitForTimeout(1500);
if (locale) await page.evaluate((l) => localStorage.setItem('bow.locale', l), locale);

const api = (path, init = {}) => page.evaluate(async ([p, i]) => {
  const token = (document.cookie.match(/auth\.token=([^;]+)/) || [])[1];
  const org = localStorage.getItem('organization_id') || JSON.parse(localStorage.getItem('organization') || '{}').id;
  const r = await fetch(`/api${p}`, {
    ...i, headers: { ...(i.headers || {}), Authorization: `Bearer ${decodeURIComponent(token || '')}`, ...(org ? { 'X-Organization-Id': org } : {}) },
  });
  return r.json();
}, [path, init]);

const openSettings = async () => {
  await page.goto(`${origin}/settings/pii`, { waitUntil: 'load' });
  await page.getByTestId('pii-policy-list').waitFor({ timeout: 60000 });
  await page.waitForTimeout(800);
};
const openAgent = async (id) => {
  await page.goto(`${origin}/agents/${id}/settings`, { waitUntil: 'load' });
  await page.getByText(/Permission mode cap|הגבלת מצב ההרשאות/).first().waitFor({ timeout: 60000 });
  await page.waitForTimeout(1200);
};
const agentRow = (id) => page.locator(`[data-testid="pii-agent-row"][data-agent-id="${id}"]`);
const saved = (pathPart) => page.waitForResponse(
  (r) => r.url().includes(pathPart) && r.request().method() === 'PUT', { timeout: 15000 });

if (mode === 'shots') {
  await openSettings();
  await shot(`${prefix}-settings`, { fullPage: true });
  const section = page.getByTestId('pii-agent-policies');
  if (await section.count()) await section.screenshot({ path: join(out, `${prefix}-settings-agents${suffix}.png`) });
  await openAgent(HR);
  await shot(`${prefix}-agent`);
  const ctl = page.getByTestId('agent-pii-policy');
  if (await ctl.count()) await ctl.screenshot({ path: join(out, `${prefix}-agent-control${suffix}.png`) });
} else if (mode === 'flow') {
  await openSettings();
  check('settings: agents section visible', await page.getByTestId('pii-agent-policies').isVisible());
  const rows = page.getByTestId('pii-agent-row');
  check('settings: one row per agent', (await rows.count()) >= 2, `${await rows.count()} rows`);

  // Default for agents → Sales (emails)
  let wait = saved('/api/organization/settings');
  await page.getByTestId('pii-default-agent-policy').selectOption('sales');
  await wait;
  // HR → HR (emails + phones)
  const hrRow = agentRow(HR);
  wait = saved('/api/organization/settings');
  await hrRow.locator('select').selectOption('hr');
  await wait;
  await page.waitForTimeout(500);
  await page.reload({ waitUntil: 'load' });
  await page.getByTestId('pii-agent-policies').waitFor();
  await page.waitForTimeout(800);
  check('settings: default agent policy persisted after reload',
    (await page.getByTestId('pii-default-agent-policy').inputValue()) === 'sales');
  check('settings: HR policy persisted after reload',
    (await agentRow(HR).locator('select').inputValue()) === 'hr');
  check('settings: Sales shows inherit',
    (await agentRow(SALES).locator('select').inputValue()) === 'inherit');
  await shot('flow-1-settings', { fullPage: true });
  await page.getByTestId('pii-agent-policies').screenshot({ path: join(out, `flow-1-settings-agents${suffix}.png`) });

  // Agent settings (Sales): shows the inherited org default, then choose HR's policy.
  await openAgent(SALES);
  const sel = page.getByTestId('agent-pii-policy-select');
  check('agent: control visible', await page.getByTestId('agent-pii-policy').isVisible());
  check('agent: inherits org default', (await sel.inputValue()) === 'inherit');
  const inheritLabel = await sel.locator('option[value="inherit"]').textContent();
  check('agent: inherit option names the default', /Sales \(emails\)/.test(inheritLabel || ''), inheritLabel);
  await page.getByTestId('agent-pii-policy').screenshot({ path: join(out, `flow-2-agent-inherit${suffix}.png`) });
  wait = saved('/pii_policy');
  await sel.selectOption('hr');
  await wait;
  await page.waitForTimeout(600);
  await page.getByTestId('agent-pii-policy').screenshot({ path: join(out, `flow-3-agent-hr${suffix}.png`) });
  await page.reload({ waitUntil: 'load' });
  await page.getByTestId('agent-pii-policy-select').waitFor({ timeout: 60000 });
  await page.waitForTimeout(800);
  check('agent: choice persisted after reload',
    (await page.getByTestId('agent-pii-policy-select').inputValue()) === 'hr');

  // Both UIs edit the same data: the PII page now shows Sales on HR's policy.
  await openSettings();
  check('settings: reflects the agent page choice',
    (await agentRow(SALES).locator('select').inputValue()) === 'hr');

  // Put Sales back on the org default from the agent page (leaves the loop's state as configured above).
  await openAgent(SALES);
  wait = saved('/pii_policy');
  await page.getByTestId('agent-pii-policy-select').selectOption('inherit');
  await wait;
  check('agent: back to inherit', (await page.getByTestId('agent-pii-policy-select').inputValue()) === 'inherit');
}

await browser.close();
const failed = results.filter((r) => !r.ok).length;
if (results.length) console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
