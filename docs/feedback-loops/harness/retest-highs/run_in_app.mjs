// Loop (b2): through the Nuxt dev server — (1) the shell pages carry the
// frame policy there too (routeRules), (2) an in-app artifact still renders in
// the report view (srcdoc iframe from utils/artifactIframe.ts).
//
//   node run_in_app.mjs <outDir> <nuxtOrigin> <apiOrigin> <adminEmail> <reportId>
import { chromium } from 'playwright';
import fs from 'node:fs';

const [outDir, nuxt, api, email, reportId] = process.argv.slice(2);
fs.mkdirSync(outDir, { recursive: true });
for (const f of ['mcp-artifact-app.html', 'mcp-visualization-app.html', 'artifact-sandbox.html', 'favicon.ico']) {
  const r = await fetch(`${nuxt}/${f}`);
  console.log(JSON.stringify({ nuxt: f, status: r.status, csp: r.headers.get('content-security-policy'),
    xfo: r.headers.get('x-frame-options') }));
}
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
await page.goto(`${nuxt}/users/sign-in`, { waitUntil: 'networkidle', timeout: 240000 }).catch(() => {});
await page.fill('#email', email);
await page.fill('#password', 'Password123!');
await Promise.all([page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 120000 }).catch(() => {}),
  page.click('button[type=submit]')]);
await page.goto(`${nuxt}/reports/${reportId}`, { waitUntil: 'domcontentloaded', timeout: 180000 });
let found = null;
for (let i = 0; i < 240 && !found; i++) {
  await page.waitForTimeout(1000);
  for (const f of page.frames()) {
    try {
      const t = await f.evaluate(() => (document.getElementById('inapp-ok') || {}).textContent || null);
      if (t) {
        found = { text: t, frameUrl: f.url() };
        await f.evaluate(() => document.getElementById('inapp-ok').scrollIntoView());
        break;
      }
    } catch (e) { /* cross-origin / detached */ }
  }
}
await page.waitForTimeout(5000);
await page.screenshot({ path: `${outDir}/in-app-artifact.png` });
console.log(JSON.stringify({ url: page.url(), inApp: found, pageErrors: errors.slice(0, 3) }));
await browser.close();
