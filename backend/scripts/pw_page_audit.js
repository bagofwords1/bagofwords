// Per-page API audit: navigate each main page (single user, warmed) and dump
// EVERY /api request with timing, so we see exactly what each page costs.
const { chromium } = require('@playwright/test');
const fs = require('fs');

const FRONT = process.env.FRONT || 'http://localhost:3000';
const EMAIL = process.env.EMAIL || 'sandbox@bow.dev';
const PW = process.env.PW || 'Password123!';
const OUT = process.env.OUT || '/tmp/pw_audit';
const TARGETS = (process.env.TARGETS || '/agents,/dashboards,/monitoring,/prompts,/queries,/scheduled-tasks,/reports').split(',');

const nowms = () => Date.now();

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.PW_CHROME || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
    args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const reqStart = new Map();
  let phase = 'boot';
  const events = [];
  page.on('request', r => reqStart.set(r, nowms()));
  const finish = async (r, failed) => {
    const t0 = reqStart.get(r); if (t0 == null) return;
    let status = failed ? 'FAIL' : '';
    if (!failed) { try { const resp = await r.response(); status = resp ? resp.status() : ''; } catch {} }
    const u = r.url();
    if (u.includes('/api/')) events.push({ url: u, method: r.method(), dur: nowms() - t0, status, phase });
    reqStart.delete(r);
  };
  page.on('requestfinished', r => finish(r, false));
  page.on('requestfailed', r => finish(r, true));

  // login
  await page.goto(FRONT, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(1500);
  const emailSel = 'input[type="email"], input[name="email"], input[placeholder*="mail" i]';
  if (await page.locator(emailSel).count()) {
    await page.fill(emailSel, EMAIL);
    await page.fill('input[type="password"]', PW);
    await Promise.all([
      page.waitForLoadState('networkidle', { timeout: 60000 }).catch(() => {}),
      page.locator('button[type="submit"], button:has-text("Sign in"), button:has-text("Log in")').first().click(),
    ]);
    await page.waitForTimeout(2500);
  }

  // warm-up (compile all routes in dev)
  for (const t of TARGETS) {
    try { await page.goto(FRONT + t, { waitUntil: 'domcontentloaded', timeout: 60000 }); } catch {}
    try { await page.waitForLoadState('networkidle', { timeout: 20000 }); } catch {}
  }

  // measured pass
  const report = {};
  for (const t of TARGETS) {
    phase = t;
    const before = events.length;
    const t0 = nowms();
    try { await page.goto(FRONT + t, { waitUntil: 'domcontentloaded', timeout: 60000 }); } catch {}
    try { await page.waitForLoadState('networkidle', { timeout: 30000 }); } catch {}
    const wall = nowms() - t0;
    const slice = events.slice(before);
    // group by normalized path
    const byPath = {};
    for (const e of slice) {
      const key = e.method + ' ' + e.url.split('?')[0].replace(FRONT, '').replace(/\/[0-9a-f-]{16,}/g, '/:id');
      byPath[key] = byPath[key] || { n: 0, max: 0, tot: 0 };
      byPath[key].n++; byPath[key].max = Math.max(byPath[key].max, e.dur); byPath[key].tot += e.dur;
    }
    const rows = Object.entries(byPath).map(([k, v]) => ({ ep: k, n: v.n, maxMs: v.max, avgMs: Math.round(v.tot / v.n) }))
      .sort((a, b) => b.maxMs - a.maxMs);
    report[t] = { wallMs: wall, apiCalls: slice.length, endpoints: rows };
    console.log(`\n### ${t}  wall=${wall}ms  apiCalls=${slice.length}`);
    for (const r of rows) console.log(`   ${String(r.maxMs).padStart(6)}ms max  n=${String(r.n).padStart(2)}  avg=${String(r.avgMs).padStart(5)}  ${r.ep}`);
  }
  fs.writeFileSync(`${OUT}/audit.json`, JSON.stringify(report, null, 2));
  console.log('\nwrote', `${OUT}/audit.json`);
  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
