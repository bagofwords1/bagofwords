// Playwright: log into the real frontend and measure page-navigation cost,
// counting the request storm and per-navigation whoami latency.
//
// Usage: node pw_nav_bench.js
//   FRONT=http://localhost:3000  EMAIL=sandbox@bow.dev  PW='Password123!'
const { chromium } = require('@playwright/test');

const FRONT = process.env.FRONT || 'http://localhost:3000';
const EMAIL = process.env.EMAIL || 'sandbox@bow.dev';
const PW = process.env.PW || 'Password123!';
const OUT = process.env.OUT || '/tmp/pw';

const NAV_TARGETS = ['/', '/dashboards', '/scheduled', '/prompts', '/queries', '/monitoring'];

function nowms() { return Date.now(); }

(async () => {
  const fs = require('fs');
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PW_CHROME || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
    args: ['--no-sandbox'],
  });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  // ---- global request/response instrumentation -------------------------
  const reqStart = new Map();
  const events = []; // {url, method, dur, status, phase}
  let phase = 'boot';
  page.on('request', r => reqStart.set(r, nowms()));
  page.on('requestfinished', async r => {
    const t0 = reqStart.get(r); if (t0 == null) return;
    let status = '';
    try { const resp = await r.response(); status = resp ? resp.status() : ''; } catch {}
    events.push({ url: r.url(), method: r.method(), dur: nowms() - t0, status, phase });
    reqStart.delete(r);
  });
  page.on('requestfailed', r => {
    const t0 = reqStart.get(r); if (t0 == null) return;
    events.push({ url: r.url(), method: r.method(), dur: nowms() - t0, status: 'FAIL', phase });
    reqStart.delete(r);
  });

  // ---- login -----------------------------------------------------------
  console.log('== loading', FRONT);
  await page.goto(FRONT, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(1500);
  // If we landed on a sign-in form, fill it.
  const emailSel = 'input[type="email"], input[name="email"], input[placeholder*="mail" i]';
  if (await page.locator(emailSel).count()) {
    console.log('== signing in as', EMAIL, 'url=', page.url());
    await page.fill(emailSel, EMAIL);
    await page.fill('input[type="password"]', PW);
    await Promise.all([
      page.waitForLoadState('networkidle', { timeout: 60000 }).catch(() => {}),
      page.locator('button[type="submit"], button:has-text("Sign in"), button:has-text("Log in")').first().click(),
    ]);
    await page.waitForTimeout(2500);
  }
  console.log('== after login url=', page.url());
  await page.screenshot({ path: `${OUT}/after-login.png` });

  // ---- warm-up pass (compile routes in dev mode; untimed) --------------
  console.log('== warm-up pass (Vite compile) ...');
  for (const target of NAV_TARGETS) {
    try { await page.goto(FRONT + target, { waitUntil: 'domcontentloaded', timeout: 60000 }); } catch {}
    try { await page.waitForLoadState('networkidle', { timeout: 20000 }); } catch {}
  }
  const warmEnd = events.length; // ignore everything before the measured loop

  // ---- navigation timing loop -----------------------------------------
  const navResults = [];
  for (const target of NAV_TARGETS) {
    phase = target;
    const before = events.length;
    const t0 = nowms();
    try {
      await page.goto(FRONT + target, { waitUntil: 'domcontentloaded', timeout: 60000 });
    } catch (e) { console.log('  nav error', target, e.message); }
    // wait for the request storm to settle (network idle-ish)
    try { await page.waitForLoadState('networkidle', { timeout: 30000 }); } catch {}
    const wall = nowms() - t0;
    const slice = events.slice(before);
    const whoamis = slice.filter(e => e.url.includes('/whoami'));
    const whoMax = whoamis.length ? Math.max(...whoamis.map(e => e.dur)) : 0;
    const apiCalls = slice.filter(e => e.url.includes('/api/'));
    navResults.push({ target, wall, reqTotal: slice.length, apiCalls: apiCalls.length,
                      whoamiCount: whoamis.length, whoamiMaxMs: whoMax });
    console.log(`  ${target.padEnd(12)} wall=${wall}ms  reqs=${slice.length} api=${apiCalls.length}  whoami=${whoamis.length}x max=${whoMax}ms`);
    await page.screenshot({ path: `${OUT}/nav${target.replace(/\//g, '_')}.png` });
  }

  // ---- summary ---------------------------------------------------------
  const byUrl = {};
  for (const e of events) {
    const key = e.url.split('?')[0].replace(FRONT, '').replace(/\/[0-9a-f-]{16,}/g, '/:id');
    byUrl[key] = byUrl[key] || { n: 0, tot: 0, max: 0 };
    byUrl[key].n++; byUrl[key].tot += e.dur; byUrl[key].max = Math.max(byUrl[key].max, e.dur);
  }
  const top = Object.entries(byUrl).sort((a, b) => b[1].max - a[1].max).slice(0, 20);
  console.log('\n== slowest endpoints (max ms) across whole session ==');
  for (const [u, s] of top) console.log(`  ${String(s.max).padStart(6)}ms  n=${String(s.n).padStart(3)}  avg=${Math.round(s.tot/s.n)}ms  ${u}`);

  const dupWhoami = events.filter(e => e.url.includes('/whoami')).length;
  console.log(`\n== total whoami calls in session: ${dupWhoami}`);
  console.log('== per-nav summary:');
  for (const r of navResults) console.log('  ', JSON.stringify(r));
  fs.writeFileSync(`${OUT}/summary.json`, JSON.stringify({ navResults, top, dupWhoami }, null, 2));

  await browser.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
