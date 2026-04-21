#!/usr/bin/env node
// Headless screenshot helper, used during i18n implementation for visual validation.
// Usage: node scripts/pw-shot.js <url> <out-path> [--auth]
const { chromium } = require('/home/user/bagofwords/frontend/node_modules/playwright');
const fs = require('fs');

const [, , url, out, ...rest] = process.argv;
if (!url || !out) {
  console.error('usage: pw-shot.js <url> <out> [--auth] [--viewport=WxH] [--wait=ms]');
  process.exit(2);
}
const useAuth = rest.includes('--auth');
const vpArg = rest.find(a => a.startsWith('--viewport='));
const waitArg = rest.find(a => a.startsWith('--wait='));
const viewport = vpArg ? vpArg.slice(11).split('x').map(Number) : [1280, 800];
const extraWait = waitArg ? Number(waitArg.slice(7)) : 0;

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  });
  const ctx = await browser.newContext({ viewport: { width: viewport[0], height: viewport[1] } });
  if (useAuth) {
    const state = JSON.parse(fs.readFileSync('/home/user/bagofwords/backend/sandbox_state.json', 'utf8'));
    const token = state.session.token;
    const orgId = state.session.org_id;
    await ctx.addInitScript(([t, o]) => {
      document.cookie = `auth.token=${t}; path=/`;
      localStorage.setItem('x-organization-id', o);
    }, [token, orgId]);
    await ctx.setExtraHTTPHeaders({
      Authorization: `Bearer ${token}`,
      'X-Organization-Id': orgId,
    });
  }
  const page = await ctx.newPage();
  page.on('pageerror', e => console.error('pageerror:', e.message));
  page.on('console', m => { if (m.type() === 'error') console.error('console.error:', m.text()); });
  await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
  if (extraWait) await page.waitForTimeout(extraWait);
  await page.screenshot({ path: out, fullPage: true });
  const dir = await page.evaluate(() => document.documentElement.getAttribute('dir'));
  const lang = await page.evaluate(() => document.documentElement.getAttribute('lang'));
  console.log(JSON.stringify({ ok: true, dir, lang, url }));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
