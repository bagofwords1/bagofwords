// measure_page_timings.mjs — capture the real network waterfall for the
// root (/) and /agents pages of a running deployment, to find slow requests.
//
// Must be run from a host that can actually reach the target (e.g. inside the
// corporate network / an allowlisted IP — the deployment firewall may reset
// connections from elsewhere).
//
// Usage:
//   npm i playwright-core            # or: yarn add -D playwright-core
//   BOW_BASE=https://bow.fattal.co.il \
//   BOW_EMAIL=you@example.com BOW_PW='secret' \
//   node tools/agent/measure_page_timings.mjs
//
// Optional: BOW_CHROME=/path/to/chrome  (defaults to a Playwright chromium).
// Output: JSON on stdout (all events + a sorted "slowest API requests" table),
//         human-readable progress on stderr. Secrets are read from env only.

import { chromium } from 'playwright-core';

const EXEC = process.env.BOW_CHROME || undefined; // let playwright-core resolve if unset
const BASE = process.env.BOW_BASE || 'https://bow.fattal.co.il';
const EMAIL = process.env.BOW_EMAIL;
const PASS = process.env.BOW_PW;
if (!EMAIL || !PASS) { console.error('Set BOW_EMAIL and BOW_PW'); process.exit(2); }

const events = [];
const inflight = new Map();
const failed = [];
let phase = 'boot';

function wire(page) {
  page.on('request', (req) => inflight.set(req, Date.now()));
  page.on('requestfailed', (req) => {
    failed.push({ phase, method: req.method(), url: req.url(),
      ms: Date.now() - (inflight.get(req) || Date.now()), err: req.failure()?.errorText });
    inflight.delete(req);
  });
  page.on('response', (res) => {
    const req = res.request();
    events.push({ phase, method: req.method(), status: res.status(), url: res.url(),
      ms: Date.now() - (inflight.get(req) || Date.now()), type: req.resourceType() });
    inflight.delete(req);
  });
}

(async () => {
  const browser = await chromium.launch({ executablePath: EXEC, headless: true, args: ['--no-sandbox'] });
  const page = await (await browser.newContext({ ignoreHTTPSErrors: true })).newPage();
  wire(page);

  phase = 'login';
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(1500);
  await page.locator('input[type="email"], input[placeholder*="mail" i]').first().fill(EMAIL);
  await page.locator('input[type="password"]').first().fill(PASS);
  const t0 = Date.now();
  await page.locator('button[type="submit"], button:has-text("Sign in"), button:has-text("Log in")').first().click();
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 45000 })
    .catch(() => console.error('login did not redirect; url=', page.url()));
  console.error('login->home ms:', Date.now() - t0);

  for (const [name, path] of [['root', '/'], ['agents', '/agents']]) {
    phase = name;
    const s = Date.now();
    await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle', timeout: 120000 })
      .catch((e) => console.error(`${name} nav err`, e.message));
    console.error(`${name} networkidle ms:`, Date.now() - s);
    await page.waitForTimeout(2000);
  }
  await browser.close();

  const api = events.filter((e) => !/\.(js|css|png|jpg|jpeg|svg|woff2?|ico|map)(\?|$)/.test(e.url));
  const slowest = [...api].sort((a, b) => b.ms - a.ms).slice(0, 30);
  console.error('\nSlowest API requests:');
  for (const e of slowest) console.error(`  ${String(e.ms).padStart(6)}ms  ${e.status}  ${e.phase.padEnd(7)} ${e.method} ${e.url.replace(BASE, '')}`);
  console.log(JSON.stringify({ slowest, failed, events }, null, 2));
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
