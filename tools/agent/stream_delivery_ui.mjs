// Browser-level check that chat tokens stream (not arrive in one burst) through a
// given network path. See docs/feedback-loops/2026-10-18-sse-buffering-middlebox.md.
//
// Needs: the stack served at BASE (e.g. middlebox -> Caddy -> uvicorn with
// SERVE_FRONTEND=1), slow_stub_llm.py as the default model, the demo data source.
//
//   cd frontend && export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
//   BASE=http://127.0.0.1:8088 LABEL=before node ../tools/agent/stream_delivery_ui.mjs <outdir>
//   RELOAD_AT_MS=3000 ...   reload mid-answer (exercises the watch stream)
//
// It samples how many "Progress marker" lines of the stub's answer are visible
// every 200 ms. Streamed: the count climbs in many steps. Buffered: it stays 0
// and then jumps to the full answer. Prints one JSON line; exit 3 = buffered.
import { chromium } from '@playwright/test';
import fs from 'fs';

const BASE = process.env.BASE || 'http://127.0.0.1:8088';
const LABEL = process.env.LABEL || 'run';
const OUT = process.argv[2] || '/tmp/bow-agent/stream-delivery';
const PROMPT = process.env.PROMPT || (
  'Analyze our quarterly revenue by region and product line. Compare against the same quarter last year, ' +
  'flag any region whose growth dropped by more than five percentage points, list the top three contributing ' +
  'products for each flagged region, and suggest two hypotheses for the decline that we could check against ' +
  'the marketing spend and headcount data. Keep the answer short and structured.');
fs.mkdirSync(OUT, { recursive: true });
const log = (...a) => console.error(new Date().toISOString().slice(11, 23), ...a);

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || '/opt/pw-browsers/chromium' });
const context = await browser.newContext({
  viewport: { width: 1280, height: 860 },
  recordVideo: { dir: OUT, size: { width: 1280, height: 860 } },
});
const page = await context.newPage();
const consoleLines = [];
page.on('console', (m) => { if (/stream|buffer/i.test(m.text())) consoleLines.push(`${m.type()}: ${m.text()}`); });
const diagRequests = [];
page.on('request', (r) => { if (r.url().includes('stream-diagnostics')) diagRequests.push({ url: r.url(), body: r.postData() }); });
let kickoffHeaders = null;
page.on('response', (r) => {
  if (r.request().method() === 'POST' && /\/api\/reports\/[^/]+\/completions$/.test(new URL(r.url()).pathname)) {
    const h = r.headers();
    kickoffHeaders = { 'content-encoding': h['content-encoding'] || null, 'cache-control': h['cache-control'] || null, via: h['via'] || null };
  }
});

// Sign in, skip onboarding.
await page.goto(`${BASE}/users/sign-in`);
await page.fill('#email', 'admin@example.com');
await page.fill('#password', 'Password123!');
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 30000 });
if (page.url().includes('/onboarding')) {
  const skip = page.getByRole('button', { name: 'Skip onboarding' });
  if (await skip.isVisible({ timeout: 10000 }).catch(() => false)) {
    await skip.click();
    await page.waitForURL((u) => !u.pathname.includes('/onboarding'), { timeout: 15000 });
  }
}

// Create a report with the demo data source through the API (same origin).
const reportId = await page.evaluate(async () => {
  const token = decodeURIComponent((document.cookie.match(/(?:^|; )auth\.token=([^;]*)/) || [])[1] || '');
  const auth = token.startsWith('Bearer') ? token : `Bearer ${token}`;
  const orgs = await (await fetch('/api/organizations', { headers: { Authorization: auth } })).json();
  const H = { Authorization: auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' };
  const ds = await (await fetch('/api/data_sources', { headers: H })).json();
  const r = await fetch('/api/reports', { method: 'POST', headers: H, body: JSON.stringify({ title: 'stream delivery', data_sources: [ds[0].id] }) });
  return (await r.json()).id;
});
log('report', reportId);

await page.goto(`${BASE}/reports/${reportId}`);
const box = page.locator('.mention-input-field').first();
await box.waitFor({ state: 'visible', timeout: 60000 });
await page.waitForTimeout(1500);
await box.click();
await box.fill(PROMPT);
const send = page.locator('button.w-7.h-7.rounded-full:not([disabled])').last();
await send.waitFor({ state: 'visible', timeout: 15000 });
const t0 = Date.now();
await send.click();
log('sent');

const samples = [];
const shots = new Set();
// RELOAD_AT_MS: reload mid-answer so the page re-attaches through the watch stream.
const RELOAD_AT_MS = Number(process.env.RELOAD_AT_MS || 0);
let reloaded = false;
while (Date.now() - t0 < 60000) {
  if (RELOAD_AT_MS && !reloaded && Date.now() - t0 >= RELOAD_AT_MS) {
    reloaded = true;
    await page.reload();
    log('reloaded');
  }
  const n = await page.evaluate(() => (document.body.innerText.match(/Progress marker \d{3}/g) || []).length);
  const done = await page.evaluate(() => document.body.innerText.includes('All done: the full answer streamed to the end'));
  const t = Date.now() - t0;
  samples.push([t, n]);
  for (const at of [3000, 6000, 9000]) {
    if (t >= at && !shots.has(at)) { shots.add(at); await page.screenshot({ path: `${OUT}/${LABEL}-t${at / 1000}s.png` }); }
  }
  if (done) break;
  await page.waitForTimeout(200);
}
await page.waitForTimeout(1500);
await page.screenshot({ path: `${OUT}/${LABEL}-final.png` });

const firstVisible = samples.find(([, n]) => n > 0);
let steps = 0;
for (let i = 1; i < samples.length; i++) if (samples[i][1] > samples[i - 1][1]) steps++;
const end = samples[samples.length - 1];
// Share of the answer visible halfway through the run.
const mid = samples.find(([t]) => t >= end[0] / 2) || end;
const out = {
  label: LABEL,
  base: BASE,
  kickoff_headers: kickoffHeaders,
  first_marker_visible_ms: firstVisible ? firstVisible[0] : null,
  answer_done_ms: end[0],
  markers_at_end: end[1],
  markers_visible_at_half_time: mid[1],
  visible_growth_steps: steps,
  verdict: steps >= 5 && mid[1] > 0 ? 'STREAMED' : 'BUFFERED',
  diagnostics_requests: diagRequests,
  console: consoleLines.slice(0, 5),
};
fs.writeFileSync(`${OUT}/${LABEL}-samples.json`, JSON.stringify({ ...out, samples }, null, 1));
console.log(JSON.stringify(out));
await context.close();
await browser.close();
process.exit(out.verdict === 'BUFFERED' ? 3 : 0);
