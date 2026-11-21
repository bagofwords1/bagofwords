// Probe the report page's run-activity dot (the black circle) across many
// streaming shapes. Pair with stream_indicator_stub_llm.py (see its header).
//
// For each scenario it opens a fresh report, sends `[scenario:<name>] ...`,
// and samples the newest assistant message every animation frame while the
// run is live (the composer's stop button is shown). Each sample records:
//   - how many dots are on screen and their size,
//   - whether any visible content sits BELOW the dot (dot not at the tail).
// A healthy run has exactly one dot, of one size, at the tail, for every
// live frame. The script prints a table and writes <out>/<label>-probe.json,
// a screenshot at the first frame of each kind of defect, and (VIDEO=1) a
// recording per scenario.
//
//   cd frontend && node ../tools/agent/stream_indicator_probe.mjs <out-dir> [label] [scenario,...]
//
// Env: BOW_ORIGIN (default http://localhost:3000), BOW_EMAIL, BOW_PASSWORD, VIDEO=1.
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';

const { chromium } = createRequire(import.meta.url)('playwright');
const ALL = ['direct', 'reasoning', 'text_tool', 'multi_tool', 'parallel', 'reason_tool', 'long_gap'];
const [OUT = 'stream-indicator', LABEL = 'after', LIST] = process.argv.slice(2);
const SCENARIOS = LIST ? LIST.split(',') : ALL;
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log('[dot-probe]', ...a);

const browser = await chromium.launch();

async function newContext(name) {
  return browser.newContext({
    viewport: { width: 1280, height: 860 },
    deviceScaleFactor: 1,
    ...(process.env.VIDEO ? { recordVideo: { dir: `${OUT}/video/${LABEL}-${name}`, size: { width: 1280, height: 860 } } } : {}),
    storageState: storage,
  });
}

// Log in once.
let storage;
{
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(`${BASE}/users/sign-in`);
  await page.fill('#email', process.env.BOW_EMAIL || 'admin@example.com');
  await page.fill('#password', process.env.BOW_PASSWORD || 'Password123!');
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
  storage = await ctx.storageState();
  await ctx.close();
}

// Runs in the page: samples once per frame into window.__dot.
function installSampler() {
  const samples = [];
  window.__dot = samples;
  const t0 = performance.now();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return null;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) return null;
    return r;
  };
  const tick = () => {
    const running = !!document.querySelector('[data-testid="stop-button"]');
    const msgs = document.querySelectorAll('[data-message-role="system"]');
    const msg = msgs[msgs.length - 1];
    if (running && msg) {
      const content = msg.querySelector('.assistant-content');
      const dots = [...(content?.querySelectorAll('span.rounded-full[aria-hidden="true"]') || [])]
        .map((el) => [el, visible(el)])
        .filter(([, r]) => r && r.width >= 8 && r.width <= 16);
      let below = 0;
      let belowWhat = '';
      if (dots.length && content) {
        const [dotEl, dotRect] = dots[dots.length - 1];
        const holder = dotEl.parentElement;
        for (const el of content.querySelectorAll('*')) {
          if (holder.contains(el) || el.closest('[data-completion-footer]')) continue;
          const ownText = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
          if (!ownText && !['CANVAS', 'IMG', 'svg', 'TABLE'].includes(el.tagName)) continue;
          const r = visible(el);
          if (r && r.top > dotRect.bottom - 1) {
            below++;
            if (!belowWhat) belowWhat = (el.textContent || el.tagName).trim().slice(0, 40);
          }
        }
      }
      const t = Math.round(performance.now() - t0);
      const len = (content?.innerText || '').length;
      if (len !== window.__lastLen) { window.__lastLen = len; window.__lastChange = t; }
      samples.push({
        t,
        // Nothing on screen has changed for 400ms: without a dot the run looks dead.
        idle: t - (window.__lastChange ?? t) > 400,
        dots: dots.length,
        size: dots.length ? Math.round(dots[0][1].width) : 0,
        y: dots.length ? Math.round(dots[0][1].top) : null,
        below,
        belowWhat,
      });
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

const results = [];
for (const name of SCENARIOS) {
  const context = await newContext(name);
  const page = await context.newPage();
  page.setDefaultNavigationTimeout(120000);
  page.on('pageerror', (e) => log(name, 'pageerror', e.message));
  await page.goto(`${BASE}/`);
  const reportId = await page.evaluate(async (title) => {
    const token = decodeURIComponent((document.cookie.match(/(?:^|; )auth\.token=([^;]*)/) || [])[1] || '');
    const auth = token.startsWith('Bearer') ? token : `Bearer ${token}`;
    const orgs = await (await fetch('/api/organizations', { headers: { Authorization: auth } })).json();
    const H = { Authorization: auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' };
    const ds = await (await fetch('/api/data_sources', { headers: H })).json();
    const r = await fetch('/api/reports', { method: 'POST', headers: H, body: JSON.stringify({ title, data_sources: [ds[0].id] }) });
    return (await r.json()).id;
  }, `Dot probe: ${name}`);
  await page.goto(`${BASE}/reports/${reportId}`);
  const box = page.locator('.mention-input-field').first();
  await box.waitFor({ state: 'visible', timeout: 120000 });
  await page.waitForTimeout(1200);
  await page.evaluate(installSampler);
  await box.click();
  await box.fill(`[scenario:${name}] What is our revenue by country?`);
  await page.locator('button.w-7.h-7.rounded-full:not([disabled])').last().click();

  // Screenshot the first frame of each defect kind while the run is live.
  const shotTaken = new Set();
  const start = Date.now();
  await page.locator('[data-testid="stop-button"]').waitFor({ timeout: 30000 }).catch(() => {});
  while (Date.now() - start < 240000) {
    if (!(await page.locator('[data-testid="stop-button"]').count())) break;
    const last = await page.evaluate(() => window.__dot[window.__dot.length - 1]);
    if (last) {
      const kind = last.dots === 0 ? (last.idle ? 'missing-idle' : 'missing') : last.below ? 'misplaced' : last.dots > 1 ? 'duplicate' : '';
      if (kind && !shotTaken.has(kind)) {
        shotTaken.add(kind);
        await page.screenshot({ path: `${OUT}/${LABEL}-${name}-${kind}.png` });
      }
    }
    await page.waitForTimeout(120);
  }
  const samples = await page.evaluate(() => window.__dot);
  const live = samples.length;
  const missing = samples.filter((s) => s.dots === 0).length;
  const idleMissing = samples.filter((s) => s.dots === 0 && s.idle).length;
  const misplaced = samples.filter((s) => s.below > 0).length;
  const dup = samples.filter((s) => s.dots > 1).length;
  const sizes = [...new Set(samples.filter((s) => s.size).map((s) => s.size))];
  let toggles = 0;
  for (let i = 1; i < samples.length; i++) if ((samples[i].dots > 0) !== (samples[i - 1].dots > 0)) toggles++;
  // Longest stretch without a dot, in ms.
  let gap = 0;
  let gapStart = null;
  for (const s of samples) {
    if (s.dots === 0) { gapStart ??= s.t; gap = Math.max(gap, s.t - gapStart); } else gapStart = null;
  }
  const firstMisplaced = samples.find((s) => s.below > 0);
  const row = {
    scenario: name, live_frames: live,
    missing_pct: live ? +(100 * missing / live).toFixed(1) : 0,
    idle_missing_pct: live ? +(100 * idleMissing / live).toFixed(1) : 0,
    longest_gap_ms: gap,
    misplaced_pct: live ? +(100 * misplaced / live).toFixed(1) : 0,
    duplicate_frames: dup, appear_disappear: toggles, sizes_px: sizes.join('/'),
    misplaced_under: firstMisplaced?.belowWhat || '',
  };
  results.push(row);
  log(JSON.stringify(row));
  await page.waitForTimeout(1500);
  await context.close();
}
console.table(results);
writeFileSync(`${OUT}/${LABEL}-probe.json`, JSON.stringify(results, null, 1));
await browser.close();
