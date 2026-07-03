// Playwright: open the PUBLIC report page (/r/{id}) for a small and a large
// report and record what the browser actually experiences — per-request
// latency of every /api call (the DevTools "Network" view) and the total
// time until the artifact iframe renders its charts.
//
// Companion to backend/scripts/seed_artifact_perf_repro.py, which prints
// SMALL_REPORT_ID / LARGE_REPORT_ID.
//
// Usage:
//   NODE_PATH=<dir-with-playwright-core> \
//   FRONT=http://localhost:3000 SMALL=<id> LARGE=<id> OUT=/tmp/pw \
//   node backend/scripts/pw_artifact_perf_repro.js
const { chromium } = require('playwright-core');
const fs = require('fs');

const FRONT = process.env.FRONT || 'http://localhost:3000';
const SMALL = process.env.SMALL;
const LARGE = process.env.LARGE;
const OUT = process.env.OUT || '/tmp/pw';
const CHROME = process.env.PW_CHROME || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';

function shortUrl(u, reportId) {
  return u
    .replace(FRONT, '')
    .replace(reportId, '{id}')
    .replace(/[0-9a-f]{8}-[0-9a-f-]{27,}/g, '{uuid}');
}

async function measure(browser, label, reportId) {
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
    apiCalls.push({ url: shortUrl(r.url(), reportId), ms: Date.now() - t0, status, size });
  });

  const t0 = Date.now();
  await page.goto(`${FRONT}/r/${reportId}`, { waitUntil: 'domcontentloaded', timeout: 600000 });
  const tDom = Date.now() - t0;

  // The page shows "Loading..." until report + artifacts + EVERY query's /step
  // have been fetched; only then is the iframe srcdoc computed.
  await page.waitForSelector('iframe', { state: 'attached', timeout: 600000 });
  const tIframe = Date.now() - t0;

  // Wait for the charts inside the iframe (real render, not just srcdoc set).
  let tCharts = null;
  try {
    await page.frameLocator('iframe').locator('canvas').first()
      .waitFor({ state: 'visible', timeout: 120000 });
    tCharts = Date.now() - t0;
  } catch {}
  await page.waitForTimeout(1500); // let all four charts paint

  fs.mkdirSync(OUT, { recursive: true });
  const shot = `${OUT}/artifact-${label}.png`;
  await page.screenshot({ path: shot, fullPage: false });

  console.log(`\n=== ${label} report (${reportId}) ===`);
  console.log(`DOMContentLoaded: ${tDom} ms`);
  console.log(`iframe (all API data fetched): ${tIframe} ms`);
  console.log(`charts rendered in iframe: ${tCharts == null ? 'n/a' : tCharts + ' ms'}`);
  console.log(`screenshot: ${shot}`);
  console.log('api requests (browser-observed, like the DevTools Network tab):');
  for (const c of apiCalls.sort((a, b) => b.ms - a.ms)) {
    console.log(`  ${String(c.ms).padStart(7)} ms  ${String(c.status).padStart(3)}  ${(c.size / 1024).toFixed(1).padStart(9)} kB  ${c.url}`);
  }
  await ctx.close();
  return { label, tDom, tIframe, tCharts, apiCalls };
}

(async () => {
  if (!SMALL || !LARGE) {
    console.error('Set SMALL=<report_id> and LARGE=<report_id> (from seed_artifact_perf_repro.py)');
    process.exit(1);
  }
  const browser = await chromium.launch({
    headless: true,
    executablePath: CHROME,
    args: ['--no-sandbox'],
  });

  // Warm-up: first hit compiles the Nuxt dev route; do it on the small report
  // and discard, so dev-server compile time doesn't pollute the comparison.
  const warm = await browser.newPage();
  await warm.goto(`${FRONT}/r/${SMALL}`, { waitUntil: 'domcontentloaded', timeout: 600000 });
  await warm.waitForSelector('iframe', { state: 'attached', timeout: 600000 }).catch(() => {});
  await warm.close();

  const small = await measure(browser, 'small', SMALL);
  const large = await measure(browser, 'large', LARGE);

  const ratio = (large.tIframe / small.tIframe).toFixed(1);
  console.log(`\n=== summary ===`);
  console.log(`time to artifact iframe: small=${small.tIframe} ms, large=${large.tIframe} ms (${ratio}x slower)`);
  await browser.close();
})();
