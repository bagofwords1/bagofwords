// Count ECharts (re)initialisations of create_data chart cards on /reports/[id].
// Every VChart mount creates a new echarts instance with a fresh
// `_echarts_instance_` attribute, so distinct ids per card == mounts. Each card
// should mount exactly once — while the chart first appears during a live run,
// and again on a report reload. Exits 1 if any card mounts more than once.
// Feedback loop: docs/feedback-loops/chart-preview-remounts.md
//
//   DS_ID=<sqlite source id> node tools/agent/verify_chart_remounts.mjs <label> [outDir]
import { chromium } from 'playwright';
import fs from 'fs';

const [label = 'run', OUT = '/tmp/bow-agent/chartprobe'] = process.argv.slice(2);
const BASE = 'http://localhost:3000';
const DS_ID = process.env.DS_ID;
if (!DS_ID) { console.error('DS_ID (sqlite_source_1 id) required'); process.exit(2); }
fs.mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log(`[${label}]`, ...a);

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const context = await browser.newContext({
  viewport: { width: 1280, height: 860 },
  recordVideo: { dir: `${OUT}/video-${label}`, size: { width: 1280, height: 860 } },
});
await context.addInitScript(() => {
  const w = window;
  w.__charts = [];           // { id, t, rows } first time each instance id is seen
  w.__spinnerSeen = false;
  const seen = new Set();
  const t0 = performance.now();
  setInterval(() => {
    document.querySelectorAll('[_echarts_instance_]').forEach((el) => {
      const id = el.getAttribute('_echarts_instance_');
      if (seen.has(id)) return;
      seen.add(id);
      w.__charts.push({ id, t: Math.round(performance.now() - t0), card: el.closest('.tool-execution-container')?.getAttribute('data-step-id') || el.closest('.tool-execution-container')?.parentElement?.id || '?' });
    });
    if (document.querySelector('[data-testid="chart-awaiting-rows"]')) w.__spinnerSeen = true;
  }, 25);
});
const page = await context.newPage();
page.on('pageerror', (e) => log('PAGE ERROR:', e.message));

await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle', timeout: 180000 });
await page.waitForSelector('#email', { timeout: 180000 });
await page.waitForTimeout(3000);
await page.fill('#email', 'admin@example.com');
await page.fill('#password', 'Password123!');
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 180000 });

const token = await page.evaluate(async () => {
  const r = await fetch('/api/auth/jwt/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'username=admin%40example.com&password=Password123!',
  });
  return (await r.json()).access_token;
});
const orgId = await page.evaluate(async (t) => {
  const r = await fetch('/api/organizations', { headers: { Authorization: `Bearer ${t}` } });
  return (await r.json())[0].id;
}, token);
const report = await page.evaluate(async ({ t, org, ds }) => {
  const r = await fetch('/api/reports', {
    method: 'POST',
    headers: { Authorization: `Bearer ${t}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: 'Chart remount probe', files: [], data_sources: [ds] }),
  });
  return await r.json();
}, { t: token, org: orgId, ds: DS_ID });
log('report', report.id);

// ---- Scenario 1: chart first appears during a live run ----
await page.goto(`${BASE}/reports/${report.id}`, { waitUntil: 'domcontentloaded' });
const input = page.locator('div.mention-input-field[contenteditable="true"]').first();
await input.waitFor({ state: 'visible', timeout: 120000 });
await page.waitForTimeout(2500);
await page.evaluate(() => { window.__charts.length = 0 })
await input.fill('Show amount by customer as a bar chart');
await input.press('Enter');
// Wait for the stream to finish: the post-[DONE] completions reload is the
// last thing that can touch the card.
await page.waitForResponse(
  (r) => /\/api\/reports\/[^/]+\/completions/.test(r.url()) && r.request().method() === 'GET',
  { timeout: 180000 },
).catch(() => {});
await page.locator('[_echarts_instance_]').first().waitFor({ timeout: 120000 });
await page.waitForTimeout(8000);
const perCard = (charts) => { const m = {}; for (const c of charts) m[c.card] = (m[c.card] || 0) + 1; return Object.values(m) };
const live = await page.evaluate(() => ({ charts: window.__charts, spinnerSeen: window.__spinnerSeen }));
const liveRows = await page.locator('[data-testid="widget-row-count"]').first().innerText().catch(() => '?');
await page.screenshot({ path: `${OUT}/${label}-live.png` });
log('LIVE  mounts:', live.charts.length, 'per card:', JSON.stringify(perCard(live.charts)), 'at ms', live.charts.map((c) => c.t).join(','), '| rows label:', liveRows, '| spinner:', live.spinnerSeen);

// ---- Scenario 2: report reload ----
await page.reload({ waitUntil: 'domcontentloaded' });
await page.locator('[_echarts_instance_]').first().waitFor({ timeout: 120000 });
await page.waitForTimeout(8000);
const reload = await page.evaluate(() => ({ charts: window.__charts, spinnerSeen: window.__spinnerSeen }));
const reloadRows = await page.locator('[data-testid="widget-row-count"]').first().innerText().catch(() => '?');
await page.screenshot({ path: `${OUT}/${label}-reload.png` });
log('RELOAD mounts:', reload.charts.length, 'per card:', JSON.stringify(perCard(reload.charts)), 'at ms', reload.charts.map((c) => c.t).join(','), '| rows label:', reloadRows, '| spinner:', reload.spinnerSeen);

fs.writeFileSync(`${OUT}/${label}.json`, JSON.stringify({ report: report.id, live, liveRows, reload, reloadRows }, null, 1));
await context.close();
await browser.close();
const worst = Math.max(...perCard(live.charts), ...perCard(reload.charts));
log(worst === 1 ? 'PASS: every chart card mounted once' : `FAIL: a chart card mounted ${worst} times`);
process.exit(worst === 1 ? 0 : 1);
