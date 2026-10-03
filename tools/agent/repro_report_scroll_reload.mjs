// Repro: report page chat scroll jump on load + dashboard iframe reloads.
// See docs/feedback-loops/report-scroll-jump-artifact-reload.md.
//
//   cd frontend && node ../tools/agent/repro_report_scroll_reload.mjs <report_id> [mobile|desktop] [userScrollAtMs]
//
// Prints a summary (scroll positions, iframe srcdoc sets / loads) and a
// timeline: every chat scroll event, programmatic scrollTop writes, the
// user's wheel gesture, iframe loads and the refresh-on-view response.
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');
const [reportId, mode = 'mobile', scrollAt = '300'] = process.argv.slice(2);
const origin = 'http://localhost:3000';
const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const ctx = await browser.newContext(mode === 'mobile'
  ? { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 }
  : { viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', 'admin@example.com');
await page.fill('#password', 'Password123!');
await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}), page.click('button[type=submit]')]);
await page.waitForTimeout(1500);
if (page.url().includes('/onboarding')) { try { await page.getByText('Skip onboarding').click({ timeout: 4000 }); await page.waitForTimeout(1000); } catch {} }

// Instrumentation installed before any page script runs.
await page.addInitScript(() => {
  const t0 = performance.now();
  const log = (window.__log = []);
  const ev = (k, d) => log.push({ t: Math.round(performance.now() - t0), k, ...d });
  window.__ev = ev;
  const seen = new WeakSet();
  const hook = () => {
    const sc = document.querySelector('.chat-messages');
    if (sc && !seen.has(sc)) { seen.add(sc); ev('chat-container-mounted', {});
      sc.addEventListener('scroll', () => ev('scroll', { top: Math.round(sc.scrollTop), max: sc.scrollHeight - sc.clientHeight, user: !!window.__userScrolling }), { passive: true }); }
    document.querySelectorAll('iframe[data-artifact-frame]').forEach((f) => {
      if (seen.has(f)) return; seen.add(f); ev('iframe-mounted', {});
      f.addEventListener('load', () => ev('iframe-load', { len: (f.getAttribute('srcdoc') || '').length }));
      new MutationObserver(() => ev('iframe-srcdoc-set', { len: (f.getAttribute('srcdoc') || '').length })).observe(f, { attributes: true, attributeFilter: ['srcdoc'] });
    });
  };
  const _ael = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, fn, opts) {
    if (type === 'scroll' && this.classList && this.classList.contains('chat-messages') && fn && fn.name === 'onScroll') { window.__onScrollSeen = true; ev('PAGE-onScroll-ATTACHED', {}); }
    if (type === 'resize' && this === window && fn && /follow/i.test(fn.name)) ev('PAGE-resize-follow-ATTACHED', {});
    return _ael.call(this, type, fn, opts); };
  const _st2 = window.setTimeout;
  window.setTimeout = function (fn, d, ...a) { if (fn && fn.name === 'forceScrollToBottom') ev('scheduleInitialScroll-timer', { d }); return _st2.call(window, fn, d, ...a); };
  const desc = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
  Object.defineProperty(Element.prototype, 'scrollTop', { configurable: true, get() { return desc.get.call(this); }, set(v) {
    if (this.classList && this.classList.contains('chat-messages')) {
      const fr = (new Error().stack || '').split('\n').slice(2, 7).map(l => (l.match(/at (\S+)/) || [])[1]).filter(Boolean).join(' < ');
      ev('SCROLLTOP-WRITE', { v: Math.round(v), from: Math.round(desc.get.call(this)), listenerAttached: !!window.__onScrollSeen });
    }
    desc.set.call(this, v); } });
  new MutationObserver(hook).observe(document, { childList: true, subtree: true });
});
page.on('request', (r) => { if (/\/rerun|\/artifacts|\/completions/.test(r.url())) page.evaluate((u) => window.__ev?.('req', { u }), r.method() + ' ' + r.url().replace(/^.*\/api/, '')).catch(() => {}); });

page.on('response', async (r) => { if (/\/rerun/.test(r.url())) { const b = await r.text().catch(() => ''); page.evaluate((x) => window.__ev?.('rerun-resp', { b: x }), b.slice(0, 160)).catch(() => {}); } });
await page.goto(`${origin}/reports/${reportId}`);
// Wait until chat content is visible, then behave like a user: scroll up.
try { await page.waitForSelector('.chat-messages', { timeout: 90000 }); } catch (e) { await page.screenshot({ path: 'repro-report-debug.png' }); console.log('FAIL url', page.url()); process.exit(1); }
await page.waitForFunction(() => { const s = document.querySelector('.chat-messages'); return s && s.scrollHeight > s.clientHeight * 2; }, null, { timeout: 30000 });
await page.waitForTimeout(Number(scrollAt));
const before = await page.evaluate(() => { const s = document.querySelector('.chat-messages'); return { top: s.scrollTop, max: s.scrollHeight - s.clientHeight }; });
// User gesture: real wheel/touch-equivalent scroll up 1500px in steps.
await page.evaluate(() => { window.__userScrolling = true; window.__ev('USER-SCROLL-START', {}); });
const box = await page.locator('.chat-messages').boundingBox();
await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
for (let i = 0; i < 10; i++) { await page.mouse.wheel(0, -150); await page.waitForTimeout(16); }
await page.evaluate(() => { window.__userScrolling = false; const s = document.querySelector('.chat-messages'); window.__ev('USER-SCROLL-END', { top: Math.round(s.scrollTop) }); });
const afterUser = await page.evaluate(() => document.querySelector('.chat-messages').scrollTop);
await page.waitForTimeout(6000);
const settled = await page.evaluate(() => { const s = document.querySelector('.chat-messages'); return { top: s.scrollTop, max: s.scrollHeight - s.clientHeight }; });

let tabSwitch = null;
if (mode === 'mobile') {
  // Visit the Dashboard tab and back (mobile tab bar), then repeat the user scroll.
  const dash = page.getByRole('button', { name: /dashboard/i }).first();
  try {
    await page.evaluate(() => window.__ev('TAB->dashboard', {}));
    await dash.click({ timeout: 5000 }); await page.waitForTimeout(4000);
    await page.evaluate(() => window.__ev('TAB->chat', {}));
    await page.getByRole('button', { name: /^chat$/i }).first().click({ timeout: 5000 }); await page.waitForTimeout(2500);
    const b2 = await page.locator('.chat-messages').boundingBox();
    await page.mouse.move(b2.x + b2.width / 2, b2.y + b2.height / 2);
    for (let i = 0; i < 10; i++) { await page.mouse.wheel(0, -150); await page.waitForTimeout(16); }
    const up = await page.evaluate(() => document.querySelector('.chat-messages').scrollTop);
    await page.setViewportSize({ width: 390, height: 800 }); // URL bar hide/show => resize
    await page.waitForTimeout(800);
    const afterResize = await page.evaluate(() => { const s = document.querySelector('.chat-messages'); return { top: s.scrollTop, max: s.scrollHeight - s.clientHeight }; });
    tabSwitch = { afterUserScrollTop: up, afterResize };
  } catch (e) { tabSwitch = { error: String(e).slice(0, 200) }; }
}
const log = await page.evaluate(() => window.__log);
const compact = [];
for (const e of log) { const last = compact[compact.length - 1]; if (e.k === 'scroll' && last?.k === 'scroll' && last.user === e.user && e.t - last.t < 30) { compact[compact.length - 1] = e; continue; } compact.push(e); }
console.log(JSON.stringify({ mode, before, afterUser, settled, tabSwitch,
  iframeSrcdocSets: log.filter(e => e.k === 'iframe-srcdoc-set').length,
  iframeLoads: log.filter(e => e.k === 'iframe-load').length,
  iframeMounts: log.filter(e => e.k === 'iframe-mounted').length }, null, 1));
for (const e of compact) console.log(JSON.stringify(e));
await browser.close();
