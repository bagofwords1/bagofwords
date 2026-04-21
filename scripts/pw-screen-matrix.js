#!/usr/bin/env node
// Screenshot a list of screens in en + he for visual comparison.
const { chromium } = require('/home/user/bagofwords/frontend/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const state = JSON.parse(fs.readFileSync('/home/user/bagofwords/backend/sandbox_state.json', 'utf8'));
const TOKEN = state.session.token;
const ORG_ID = state.session.org_id;

// [name, url, needsAuth]
const SCREENS = [
  ['signin',   'http://localhost:3000/users/sign-in',  false],
  ['signup',   'http://localhost:3000/users/sign-up',  false],
  ['smoke',    'http://localhost:3000/i18n-smoke',     false],
  ['home',     'http://localhost:3000/',                true],
  ['reports',  'http://localhost:3000/reports',         true],
  ['data',     'http://localhost:3000/data',            true],
  ['settings_general', 'http://localhost:3000/settings/general', true],
];

const OUT_DIR = process.argv[2] || '/tmp/shots';
fs.mkdirSync(OUT_DIR, { recursive: true });

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  });
  const summary = [];
  for (const locale of ['en', 'he']) {
    for (const [name, url, authed] of SCREENS) {
      const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
      await ctx.addInitScript((args) => {
        try {
          localStorage.setItem('bow.locale', args.locale);
          if (args.orgId) localStorage.setItem('x-organization-id', args.orgId);
          if (args.token) localStorage.setItem('auth.token', args.token);
        } catch {}
      }, { locale, orgId: authed ? ORG_ID : null, token: authed ? TOKEN : null });
      if (authed) {
        await ctx.setExtraHTTPHeaders({
          Authorization: `Bearer ${TOKEN}`,
          'X-Organization-Id': ORG_ID,
        });
        await ctx.addCookies([{
          name: 'auth.token',
          value: TOKEN,
          domain: 'localhost',
          path: '/',
        }, {
          name: 'auth_token',
          value: `Bearer ${TOKEN}`,
          domain: 'localhost',
          path: '/',
        }]);
      }
      const page = await ctx.newPage();
      page.on('pageerror', e => console.error(`[${locale}/${name}] pageerror:`, e.message));
      try {
        await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
      } catch (e) {
        console.error(`[${locale}/${name}] goto error:`, e.message);
      }
      await page.waitForTimeout(1500);
      const info = await page.evaluate(() => ({
        url: location.href,
        dir: document.documentElement.getAttribute('dir'),
        lang: document.documentElement.getAttribute('lang'),
      }));
      const out = path.join(OUT_DIR, `${locale}_${name}.png`);
      await page.screenshot({ path: out, fullPage: false });
      summary.push({ locale, name, ...info, out });
      await ctx.close();
    }
  }
  console.log(JSON.stringify(summary, null, 2));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
