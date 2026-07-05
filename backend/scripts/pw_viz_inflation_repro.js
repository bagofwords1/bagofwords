// Playwright: open the PUBLIC report page (/r/{id}) and record every /api
// request the browser makes — specifically how many /step payloads the page
// downloads for the artifact. Companion to
// backend/scripts/repro_edit_artifact_viz_inflation_live.py (prints the
// report id); browser-side evidence for the visualization_ids inflation leak.
//
// Usage:
//   NODE_PATH=frontend/node_modules FRONT=http://localhost:3000 \
//   REPORT=<report_id> OUT=/tmp/pw \
//   node backend/scripts/pw_viz_inflation_repro.js
const { chromium } = require('playwright-core');
const fs = require('fs');

const FRONT = process.env.FRONT || 'http://localhost:3000';
const REPORT = process.env.REPORT;
const OUT = process.env.OUT || '/tmp/pw';
const CHROME = process.env.PW_CHROME || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';

function shortUrl(u) {
  return u
    .replace(FRONT, '')
    .replace(REPORT, '{id}')
    .replace(/[0-9a-f]{8}-[0-9a-f-]{27,}/g, '{uuid}');
}

(async () => {
  if (!REPORT) {
    console.error('Set REPORT=<report_id> (from repro_edit_artifact_viz_inflation_live.py)');
    process.exit(1);
  }
  const browser = await chromium.launch({ headless: true, executablePath: CHROME, args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await ctx.newPage();

  const reqStart = new Map();
  const apiCalls = [];
  page.on('request', (r) => reqStart.set(r, Date.now()));
  page.on('requestfinished', async (r) => {
    const t0 = reqStart.get(r);
    if (t0 == null || !r.url().includes('/api/')) return;
    let status = '', size = 0;
    try {
      const resp = await r.response();
      status = resp ? resp.status() : '';
      size = resp ? (await resp.body()).length : 0;
    } catch {}
    apiCalls.push({ url: shortUrl(r.url()), ms: Date.now() - t0, status, size });
  });

  const t0 = Date.now();
  await page.goto(`${FRONT}/r/${REPORT}`, { waitUntil: 'domcontentloaded', timeout: 600000 });
  await page.waitForSelector('iframe', { state: 'attached', timeout: 600000 });
  const tIframe = Date.now() - t0;
  await page.waitForTimeout(3000); // let charts paint

  fs.mkdirSync(OUT, { recursive: true });
  const shot = `${OUT}/viz-inflation-r-page.png`;
  await page.screenshot({ path: shot, fullPage: false });

  const stepCalls = apiCalls.filter((c) => c.url.includes('/step'));
  const stepBytes = stepCalls.reduce((s, c) => s + c.size, 0);

  console.log(`\n=== public /r page (${REPORT}) ===`);
  console.log(`iframe ready (all API data fetched): ${tIframe} ms`);
  console.log(`screenshot: ${shot}`);
  console.log('api requests (browser-observed):');
  for (const c of apiCalls.sort((a, b) => b.size - a.size)) {
    console.log(`  ${String(c.ms).padStart(6)} ms  ${String(c.status).padStart(3)}  ${(c.size / 1024).toFixed(1).padStart(9)} kB  ${c.url}`);
  }
  console.log(`\n/step requests: ${stepCalls.length}, total ${(stepBytes / 1024).toFixed(1)} kB`);
  await browser.close();
})();
