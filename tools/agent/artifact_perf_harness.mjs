// Artifact load-performance + behavior harness.
//
// Loads every seeded artifact (tools/agent/seed_artifact_perf.py) on every
// surface (/reports/:id, /r/:id) as every role (owner, member, anonymous),
// under an emulated network, and records for each case:
//   - timings: first artifact paint inside the iframe, ARTIFACT_READY/ERROR
//   - the request waterfall (API + /libs)
//   - a behavior fingerprint of the rendered iframe: visible text, per
//     data-testid text + computed style, element counts (canvas/svg/rows),
//     ARTIFACT_ERROR payloads, console errors
//   - interaction results (tabs, table search, buttons, filters)
//   - a screenshot of the artifact iframe
//
// Run once before a change and once after, then diff with
// tools/agent/artifact_perf_compare.py.
//
//   node tools/agent/artifact_perf_harness.mjs --seed seed.json --out out/before \
//        [--origin http://localhost:3000] [--latency 60] [--down-mbps 30] [--only exec,params]
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const { chromium } = await import('playwright').catch(() => import(process.env.PLAYWRIGHT_MODULE || '/opt/node-tools/node_modules/playwright/index.mjs'));

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, arr) => {
  if (a.startsWith('--')) acc.push([a.slice(2), arr[i + 1] && !arr[i + 1].startsWith('--') ? arr[i + 1] : 'true']);
  return acc;
}, []));
const origin = args.origin || 'http://localhost:3000';
const seed = JSON.parse(readFileSync(args.seed, 'utf8'));
const outDir = args.out || 'out';
const latency = Number(args.latency ?? 60);
const downMbps = Number(args['down-mbps'] ?? 30);
const only = args.only ? new Set(args.only.split(',')) : null;
const repeats = Number(args.repeats ?? 1);
mkdirSync(join(outDir, 'shots'), { recursive: true });

const USERS = {
  owner: { email: 'admin@example.com', password: 'Password123!' },
  member: { email: 'viewer@example.com', password: 'Password123!' },
};

const exe = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const browser = await chromium.launch({ executablePath: exe });

async function storageFor(role) {
  if (role === 'anon') return undefined;
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(`${origin}/users/sign-in`, { waitUntil: 'domcontentloaded' });
  await page.fill('#email', USERS[role].email);
  await page.fill('#password', USERS[role].password);
  await Promise.all([
    page.waitForURL((u) => !String(u).includes('sign-in'), { timeout: 30000 }).catch(() => {}),
    page.click('button[type=submit]'),
  ]);
  await page.waitForTimeout(1500);
  if (page.url().includes('/onboarding')) {
    await page.getByText('Skip onboarding', { exact: false }).click({ timeout: 4000 }).catch(() => {});
    await page.waitForTimeout(1000);
  }
  const state = await ctx.storageState();
  await ctx.close();
  return state;
}

const storage = { owner: await storageFor('owner'), member: await storageFor('member'), anon: undefined };

// Records ARTIFACT_* messages the iframe posts to the host, timestamped from
// navigation start, plus the first moment #root holds real content.
const INIT = `
  window.__perf = { msgs: [], firstPaint: null };
  window.addEventListener('message', (e) => {
    const t = e && e.data && e.data.type;
    if (typeof t === 'string' && t.startsWith('ARTIFACT_')) {
      let payload = null; try { payload = JSON.parse(JSON.stringify(e.data)); } catch (_) {}
      window.__perf.msgs.push({ type: t, at: performance.now(), payload });
    }
  }, true);
  (function poll() {
    try {
      const f = document.querySelector('iframe[srcdoc]');
      const d = f && f.contentDocument;
      const root = d && d.getElementById('root');
      if (root && root.children.length && !/^(Loading|Loading\\.\\.\\.)$/.test((root.innerText || '').trim()) && root.querySelector('*:not(div:only-child)')) {
        if (!window.__perf.firstPaint) window.__perf.firstPaint = performance.now();
      }
    } catch (_) {}
    if (!window.__perf.firstPaint) setTimeout(poll, 20);
  })();
`;

const FINGERPRINT = () => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  // The artifact's own content: #root for pages, the body for HTML slides.
  // (Never the body of a page — in some error states it renders the
  // document's <script> source, which is not what the viewer sees.)
  const root = document.getElementById('root');
  const out = { text: norm((root || document.body).innerText), testids: {}, counts: {}, styles: {}, globals: {} };
  for (const el of document.querySelectorAll('[data-testid]')) {
    const cs = getComputedStyle(el);
    out.testids[el.dataset.testid] = norm(el.innerText);
    out.styles[el.dataset.testid] = [cs.display, cs.color, cs.backgroundColor, cs.fontSize, cs.fontWeight, cs.padding].join('|');
  }
  for (const sel of ['canvas', 'svg', 'tr', 'button', 'img', 'h1', 'h2', 'li', 'table']) out.counts[sel] = document.querySelectorAll(sel).length;
  // Tailwind applied at all? (a bare document has no padding on these)
  const sample = [...document.querySelectorAll('#root [class]')].slice(0, 40).map((el) => {
    const cs = getComputedStyle(el); return `${el.tagName}:${cs.display}:${cs.paddingTop}:${cs.borderRadius}:${cs.gap}`;
  });
  out.styles.__sample = sample.join(';');
  out.counts.canvasPainted = [...document.querySelectorAll('canvas')].filter((c) => {
    try { const ctx = c.getContext('2d'); if (!ctx) return true; const d = ctx.getImageData(0, 0, Math.min(c.width, 200), Math.min(c.height, 200)).data; for (let i = 3; i < d.length; i += 4) if (d[i]) return true; return false; } catch (_) { return true; }
  }).length;
  out.globals = { react: typeof window.React, echarts: typeof window.echarts, lucide: typeof window.lucide, html_dark: document.documentElement.classList.contains('dark') };
  return out;
};

async function artifactFrame(page) {
  const handle = await page.$('iframe[srcdoc]');
  return handle ? { handle, frame: await handle.contentFrame() } : null;
}

async function interact(name, frame) {
  const res = {};
  const click = async (sel) => { const el = await frame.$(sel); if (el) { await el.click(); await frame.waitForTimeout(400); return true; } return false; };
  if (name === 'exec') {
    for (const label of ['Mix', 'Regions', 'Trend']) {
      const btn = frame.locator('[data-testid=exec-root]').getByText(label, { exact: true }).first();
      if (await btn.count()) { await btn.click(); await frame.waitForTimeout(500); }
      res[`tab_${label}`] = await frame.evaluate(() => ({ canvases: document.querySelectorAll('canvas').length, title: [...document.querySelectorAll('h2,h3')].map((h) => h.innerText).join('|') }));
    }
    const search = await frame.$('input[type=search], input[placeholder*="earch"]');
    if (search) {
      await search.fill('ORD-100097');
      await frame.waitForTimeout(500);
      res.search = await frame.evaluate(() => [...document.querySelectorAll('tbody tr')].map((r) => r.innerText.replace(/\s+/g, ' ').trim()).slice(0, 5));
    }
  }
  if (name === 'edge') {
    await click('[data-testid=inc]'); await click('[data-testid=inc]');
    res.after_clicks = await frame.evaluate(() => document.querySelector('[data-testid=inc]')?.innerText);
  }
  if (name === 'params') {
    // Open the Region select and choose East if the control is interactive.
    const trigger = frame.locator('[data-testid=params-root] button, [data-testid=params-root] select').first();
    if (await trigger.count()) {
      const tag = await trigger.evaluate((el) => el.tagName);
      if (tag === 'SELECT') await trigger.selectOption({ label: 'East region' }).catch(() => {});
      else { await trigger.click().catch(() => {}); await frame.waitForTimeout(300); await frame.getByText('East region', { exact: true }).first().click().catch(() => {}); }
      await frame.waitForTimeout(3500);
      res.after_filter = await frame.evaluate(() => ({ total: document.querySelector('[data-testid=total]')?.innerText, viewer: document.querySelector('[data-testid=viewer]')?.innerText }));
    }
  }
  return res;
}

async function runCase(name, info, surface, role, iteration, ctx) {
  const page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false, latency, downloadThroughput: downMbps * 125000, uploadThroughput: 10 * 125000,
  });
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 300)); });
  page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + String(e.message).slice(0, 300)));
  const badResponses = [];
  page.on('response', (r) => { if (r.status() >= 400) badResponses.push(`${r.status()} ${r.request().method()} ${r.url().replace(origin, '')}`); });
  const reqs = [];
  let t0 = 0;
  page.on('request', (r) => { const u = r.url(); if (u.includes('/api/') || u.includes('/libs/')) reqs.push({ r, url: u.replace(origin, ''), method: r.method(), start: Date.now() - t0 }); });
  page.on('requestfinished', async (r) => { const e = reqs.find((x) => x.r === r); if (e) { e.end = Date.now() - t0; try { const resp = await r.response(); e.status = resp?.status(); e.fromCache = resp ? await resp.fromServiceWorker() : false; e.cacheControl = resp?.headers()['cache-control'] || null; const s = await r.sizes(); e.bytes = s.responseBodySize; } catch (_) {} } });
  await page.addInitScript(INIT);

  const path = surface === 'reports' ? `/reports/${info.report_id}` : `/r/${info.report_id}`;
  t0 = Date.now();
  await page.goto(origin + path, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
  const deadline = Date.now() + 45000;
  let outcome = 'timeout';
  let harnessPaint = null;
  while (Date.now() < deadline) {
    if (harnessPaint === null) {
      const fr = page.mainFrame().childFrames()[0];
      const painted = fr ? await fr.evaluate(() => {
        const root = document.getElementById('root') || (document.body && document.body.querySelector('.slide') ? document.body : null);
        if (!root) return false;
        return root.querySelectorAll('*').length > 2 && !/^Loading/.test((root.innerText || '').trim());
      }).catch(() => false) : false;
      if (painted) harnessPaint = Date.now() - t0;
    }
    const msgs = await page.evaluate(() => window.__perf?.msgs || []).catch(() => []);
    if (msgs.some((m) => m.type === 'ARTIFACT_READY')) { outcome = 'ready'; break; }
    if (msgs.some((m) => m.type === 'ARTIFACT_ERROR')) { outcome = 'error'; break; }
    // No artifact iframe at all (doc artifacts, slide previews, access walls).
    if (Date.now() - t0 > 12000 && page.mainFrame().childFrames().length === 0) { outcome = 'no_iframe'; break; }
    // Slides (and any page that never signals) — settle on paint.
    if (harnessPaint !== null && Date.now() - t0 - harnessPaint > 6000) { outcome = 'painted_no_signal'; break; }
    // Non-iframe outcomes: slides with preview images, doc mode, access walls.
    const hostState = await page.evaluate(() => {
      const t = document.body.innerText || '';
      if (/sign in|log in/i.test(t) && !document.querySelector('iframe')) return 'login_wall';
      if (/not found|don.t have access|access denied/i.test(t) && !document.querySelector('iframe')) return 'denied';
      return null;
    }).catch(() => null);
    if (hostState && Date.now() - t0 > 8000) { outcome = hostState; break; }
    await page.waitForTimeout(50);
  }
  const perf = await page.evaluate(() => window.__perf).catch(() => ({}));
  // Let refresh-on-view reruns, param runs and chart renders settle.
  await page.waitForTimeout(3000);

  const result = {
    name, surface, role, iteration, path, outcome,
    firstPaintMs: harnessPaint,
    readyMs: (perf?.msgs || []).find((m) => m.type === 'ARTIFACT_READY' || m.type === 'ARTIFACT_ERROR')?.at ?? null,
    messages: (perf?.msgs || []).map((m) => ({ type: m.type, payload: m.type === 'ARTIFACT_ERROR' ? m.payload : undefined })),
    consoleErrors,
    badResponses,
    requests: reqs.map(({ r, ...rest }) => rest),
  };

  const af = await artifactFrame(page);
  if (af && af.frame) {
    result.fingerprint = await af.frame.evaluate(FINGERPRINT).catch((e) => ({ error: String(e) }));
    result.iframeHasBabel = await af.frame.evaluate(() => typeof window.Babel !== 'undefined').catch(() => null);
    result.interactions = await interact(name, af.frame).catch((e) => ({ error: String(e) }));
    if (iteration === 0) await af.handle.screenshot({ path: join(outDir, 'shots', `${name}__${surface}__${role}.png`) }).catch(() => {});
  } else if (iteration === 0) {
    await page.screenshot({ path: join(outDir, 'shots', `${name}__${surface}__${role}.png`) }).catch(() => {});
  }
  // Host-level state around the artifact (banners, gates, error states).
  result.hostText = await page.evaluate(() => {
    const t = (document.body.innerText || '').replace(/\s+/g, ' ');
    const keep = [];
    for (const re of [/withheld[^.]*\./i, /Loading your data/i, /couldn.t load[^.]*\./i, /artifactLoadFailed/i, /No dashboard[^.]*\./i, /some charts[^.]*\./i, /Sign in[^.]*\./i]) {
      const m = t.match(re); if (m) keep.push(m[0]);
    }
    return keep;
  }).catch(() => []);
  await page.close();
  return result;
}

const results = [];
for (const [name, info] of Object.entries(seed)) {
  if (only && !only.has(name)) continue;
  for (const surface of ['r', 'reports']) {
    for (const role of ['owner', 'member', 'anon']) {
      if (surface === 'reports' && role === 'anon') continue;
      if (role === 'anon' && info.visibility !== 'public') continue;
      // One context per case = cold cache; iterations >0 reuse it (warm cache).
      const ctx = await browser.newContext({ storageState: storage[role], viewport: { width: 1400, height: 1000 } });
      for (let it = 0; it < repeats + 1; it++) {
        const res = await runCase(name, info, surface, role, it, ctx);
        res.cache = it === 0 ? 'cold' : 'warm';
        results.push(res);
        console.log(`${name.padEnd(8)} ${surface.padEnd(7)} ${role.padEnd(6)} ${res.cache} ${res.outcome.padEnd(8)} paint=${Math.round(res.firstPaintMs ?? -1)} ready=${Math.round(res.readyMs ?? -1)} errs=${res.consoleErrors.length}`);
      }
      await ctx.close();
    }
  }
}
writeFileSync(join(outDir, 'results.json'), JSON.stringify(results, null, 2));
await browser.close();
