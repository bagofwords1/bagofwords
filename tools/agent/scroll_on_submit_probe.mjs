// Measure the scroll when a prompt is sent in an existing report: one row per
// animation frame where scrollTop / scrollHeight / the pin floor / the new
// prompt's offset changed. A healthy pin eases scrollTop over ~PIN_SCROLL_MS;
// a snap shows as a single-frame jump. Pair with run_fold_stub_llm.py.
//
//   cd frontend && LABEL=after node ../tools/agent/scroll_on_submit_probe.mjs <out-dir>
//
// Env: BOW_EMAIL / BOW_PASSWORD, LABEL (screenshot prefix), ONLY_FIRST=1 (only
// shoot the first run mid-way), PW_CHROMIUM (browser path when the bundled
// Playwright's own browser is missing).
import { createRequire } from 'node:module';
const { chromium } = createRequire(process.cwd() + '/package.json')('playwright');
const OUT = process.argv[2];
const BASE = 'http://localhost:3000';
const log = (...a) => console.log('[scroll]', ...a);
const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
page.setDefaultNavigationTimeout(240000);
page.on('pageerror', (e) => log('pageerror', e.message));
await page.goto(`${BASE}/users/sign-in`);
await page.fill('#email', process.env.BOW_EMAIL || 'admin@example.com'); await page.fill('#password', process.env.BOW_PASSWORD || 'Password123!');
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 120000 });
if (page.url().includes('/onboarding')) {
  const skip = page.getByRole('button', { name: 'Skip onboarding' });
  if (await skip.isVisible({ timeout: 10000 }).catch(() => false)) await skip.click();
}
const reportId = await page.evaluate(async () => {
  const token = decodeURIComponent((document.cookie.match(/(?:^|; )auth\.token=([^;]*)/) || [])[1] || '');
  const auth = token.startsWith('Bearer') ? token : `Bearer ${token}`;
  const orgs = await (await fetch('/api/organizations', { headers: { Authorization: auth } })).json();
  const H = { Authorization: auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' };
  const ds = await (await fetch('/api/data_sources', { headers: H })).json();
  const r = await fetch('/api/reports', { method: 'POST', headers: H, body: JSON.stringify({ title: 'Scroll repro', data_sources: [ds[0].id] }) });
  return (await r.json()).id;
});
await page.goto(`${BASE}/reports/${reportId}`);
const box = page.locator('.mention-input-field').first();
await box.waitFor({ state: 'visible', timeout: 180000 });
await page.waitForTimeout(1500);
async function send(text) {
  await box.click(); await box.fill(text);
  const btn = page.locator('button.w-7.h-7.rounded-full:not([disabled])').last();
  await btn.waitFor({ state: 'visible', timeout: 15000 }); await btn.click();
}
async function waitDone(n) {
  await page.waitForFunction((n) => document.querySelectorAll('[data-completion-footer]').length >= n, n, { timeout: 180000 });
}
await send('What is our revenue by country?');
for (const ms of [2500, 2500, 2500]) { await page.waitForTimeout(ms); await page.screenshot({ path: `${OUT}/${process.env.LABEL || 'x'}-first-${Date.now()}.png` }); }
if (process.env.ONLY_FIRST) { await browser.close(); process.exit(0); }
await waitDone(1);
await page.waitForTimeout(2500);
await page.screenshot({ path: `${OUT}/${process.env.LABEL || 'x'}-0-before-submit.png` });
await page.evaluate(() => {
  const content = document.querySelector('[data-testid="transcript"]');
  let c = content.parentElement; while (c && getComputedStyle(c).overflowY !== 'auto' && getComputedStyle(c).overflowY !== 'scroll') c = c.parentElement;
  window.__c = c; window.__samples = []; const t0 = performance.now(); const n0 = document.querySelectorAll('li[data-message-id]').length;
  const tick = () => {
    const lis = document.querySelectorAll('li[data-message-id]');
    const userLi = [...lis].slice(n0).find(l => l.dataset.messageRole === 'user');
    window.__samples.push({ t: Math.round(performance.now() - t0), top: Math.round(c.scrollTop), sh: c.scrollHeight, ch: c.clientHeight,
      minH: content.style.minHeight || '', promptY: userLi ? Math.round(userLi.getBoundingClientRect().top - c.getBoundingClientRect().top) : null, n: lis.length });
    if (performance.now() - t0 < 3000) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
});
await send('And by genre?');
await page.waitForTimeout(1000);
await page.screenshot({ path: `${OUT}/${process.env.LABEL || 'x'}-1-after-submit.png` });
await page.waitForTimeout(2200);
await page.screenshot({ path: `${OUT}/${process.env.LABEL || 'x'}-2-mid-run.png` });
await page.waitForTimeout(1500);
await page.screenshot({ path: `${OUT}/${process.env.LABEL || 'x'}-3-mid-run.png` });
const s = await page.evaluate(() => window.__samples);
let last = '';
for (const x of s) { const k = `${x.top}|${x.sh}|${x.minH}|${x.promptY}|${x.n}`; if (k !== last) { console.log(JSON.stringify(x)); last = k; } }
await browser.close();
