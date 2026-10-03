// Drive the Audit Logs settings page as an org admin and capture evidence.
//
//   cd frontend
//   node ../tools/agent/audit_ui_flow.mjs <out_dir> [--phase before|after] [--locale en|he]
//
// before: the activity list only (what the page offers today).
// after:  list, detail drawer, filters (+ URL persistence), search, export
//         download, and the Streams tab (add stream → test → save → status).
//
// Env: BOW_EMAIL, BOW_PASSWORD, BOW_ORIGIN (default http://localhost:3000),
//      SIEM_MOCK (default http://127.0.0.1:8790) for the streams steps.
import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const [outDir, ...rest] = process.argv.slice(2);
if (!outDir) {
  console.error('usage: node audit_ui_flow.mjs <out_dir> [--phase before|after] [--locale en|he]');
  process.exit(2);
}
const arg = (name, dflt) => {
  const i = rest.indexOf(name);
  return i >= 0 ? rest[i + 1] : dflt;
};
const phase = arg('--phase', 'after');
const locale = arg('--locale', 'en');
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const mock = process.env.SIEM_MOCK || 'http://127.0.0.1:8790';
mkdirSync(outDir, { recursive: true });

const execPath = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const browser = await chromium.launch({ executablePath: execPath });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
const page = await context.newPage();
const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok: !!ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
};
const shot = async (name) => {
  await page.waitForTimeout(400);
  const p = join(outDir, `${phase}-${locale}-${name}.png`);
  await page.screenshot({ path: p });
  console.log(`captured ${p}`);
};

// Sign in, skip onboarding, pick the locale.
await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', email);
await page.fill('#password', password);
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.click('button[type=submit]'),
]);
await page.waitForTimeout(1200);
if (page.url().includes('/onboarding')) {
  await page.getByText('Skip onboarding', { exact: false }).click({ timeout: 4000 }).catch(() => {});
  await page.waitForTimeout(1000);
}
await page.evaluate((l) => localStorage.setItem('bow.locale', l), locale);

const gotoAudit = async (query = '') => {
  await page.goto(`${origin}/settings/audit${query}`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForSelector('[data-testid="audit-row"], .border.rounded .flex.items-center', { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(600);
};

await gotoAudit();
await shot('list');

if (phase === 'before') {
  await context.close();
  await browser.close();
  process.exit(0);
}

// ---- Row rendering: no overlap -------------------------------------------
const overlaps = await page.$$eval('[data-testid="audit-action"]', (els) =>
  els.filter((el) => el.scrollWidth > el.clientWidth + 1 && !el.title).map((el) => el.textContent.trim()),
);
const rowCount = await page.locator('[data-testid="audit-row"]').count();
check('rows render', rowCount > 0, `${rowCount} rows`);
check('no action cell overflows without a tooltip', overlaps.length === 0, overlaps.join(', '));

// ---- Detail drawer --------------------------------------------------------
const tri = page.locator('[data-testid="audit-row"]', { hasText: 'custom_query' }).first();
const triRow = (await tri.count()) ? tri : page.locator('[data-testid="audit-row"]').first();
await triRow.click();
await page.waitForSelector('[data-testid="audit-drawer"]', { timeout: 5000 }).catch(() => {});
const drawer = page.locator('[data-testid="audit-drawer"]');
check('drawer opens', await drawer.isVisible());
await shot('drawer-custom-query');
await page.keyboard.press('Escape');
await page.waitForTimeout(300);

const tool = page.locator('[data-testid="audit-row"]', { hasText: /agent|סוכן/ }).first();
if (await tool.count()) {
  await tool.click();
  await page.waitForSelector('[data-testid="audit-drawer"]', { timeout: 5000 }).catch(() => {});
  const txt = await drawer.innerText();
  check('drawer shows tool queries', /SELECT/i.test(txt));
  check('drawer shows user agent field', await drawer.locator('[data-testid="audit-field-user_agent"]').count() > 0);
  await shot('drawer-tool-event');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
} else {
  check('agent/tool row present', false);
}

// ---- Filters --------------------------------------------------------------
await page.locator('[data-testid="audit-filter-range"]').click().catch(() => {});
await page.locator('[data-testid="audit-range-24h"]').click().catch(() => {});
await page.waitForTimeout(600);
await page.locator('[data-testid="audit-filter-resource"]').click().catch(() => {});
await page.locator('[data-testid="audit-resource-option"]', { hasText: 'api_key' }).first().click().catch(() => {});
await page.keyboard.press('Escape');
await page.waitForTimeout(800);
const url = page.url();
check('filters persist in the URL', /resource_type=api_key/.test(url) && /range=24h/.test(url), url);
const filteredRows = await page.locator('[data-testid="audit-row"]').allInnerTexts();
check('resource filter narrows rows', filteredRows.length > 0 && filteredRows.every((t) => t.includes('api_key')), `${filteredRows.length} rows`);
await shot('filtered');
await page.reload({ waitUntil: 'networkidle' });
await page.waitForTimeout(800);
const afterReload = await page.locator('[data-testid="audit-row"]').allInnerTexts();
check('filters survive reload', afterReload.length === filteredRows.length);

// ---- Search by email --------------------------------------------------------
await gotoAudit();
await page.fill('[data-testid="audit-search"]', email.split('@')[0]);
await page.waitForTimeout(1000);
const searchRows = await page.locator('[data-testid="audit-row"]').count();
check('search matches user email', searchRows > 0, `${searchRows} rows`);
await shot('search-email');

// ---- Export -------------------------------------------------------------------
await gotoAudit();
const [download] = await Promise.all([
  page.waitForEvent('download', { timeout: 15000 }).catch(() => null),
  page.locator('[data-testid="audit-export-json"]').click({ timeout: 5000 }).catch(async () => {
    await page.locator('[data-testid="audit-export"]').click();
    await page.locator('[data-testid="audit-export-json"]').click();
  }),
]);
if (download) {
  const p = join(outDir, `${phase}-${locale}-export.jsonl`);
  await download.saveAs(p);
  const { readFileSync } = await import('node:fs');
  const lines = readFileSync(p, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
  check('export is envelope v1 NDJSON', lines.length > 0 && lines.every((e) => e.version === 1 && e.id && e.action), `${lines.length} events`);
} else {
  check('export downloads a file', false);
}

// ---- Streams tab ----------------------------------------------------------------
await page.locator('[data-testid="audit-tab-streams"]').click();
await page.waitForTimeout(600);
await shot('streams-empty-or-list');
await page.locator('[data-testid="stream-add"]').click();
await page.waitForTimeout(400);
await page.locator('[data-testid="stream-destination-https"]').click();
await page.fill('[data-testid="stream-name"]', 'UI Webhook');
await page.fill('[data-testid="stream-field-url"]', `${mock}/https/ui`);
await page.fill('[data-testid="stream-secret-hmac_secret"]', 'demo-hmac-secret');
await shot('stream-form');
await page.locator('[data-testid="stream-test"]').click();
await page.waitForSelector('[data-testid="stream-test-result"]', { timeout: 15000 }).catch(() => {});
const testTxt = await page.locator('[data-testid="stream-test-result"]').innerText().catch(() => '');
check('send test event succeeds', /ok|success|delivered|נשלח|הצליח/i.test(testTxt), testTxt);
await shot('stream-test-ok');
await page.locator('[data-testid="stream-save"]').click();
await page.waitForTimeout(1200);
const streamRow = page.locator('[data-testid="stream-row"]', { hasText: 'UI Webhook' });
check('stream appears in list', (await streamRow.count()) > 0);
await shot('streams-list');

writeFileSync(join(outDir, `${phase}-${locale}-results.json`), JSON.stringify(results, null, 2));
await context.close();
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
