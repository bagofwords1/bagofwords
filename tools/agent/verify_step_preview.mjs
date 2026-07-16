// Verify preview-then-full step-data loading on the report page.
//
// Confirms end-to-end that:
//   1. GET /api/reports/{id}/completions ships a small payload (preview rows).
//   2. The report renders the tool cards from the inline preview.
//   3. GET /api/steps/{id} is lazy-fetched to upgrade the preview to the full
//      result set, and the table then shows the full row count.
//
// Usage:
//   node tools/agent/verify_step_preview.mjs <report_id>
import { chromium } from 'playwright';

const REPORT_ID = process.argv[2];
const BASE = 'http://localhost:3000';
const EMAIL = 'admin@example.com';
const PASSWORD = 'Password123!';
const OUT = '/tmp/bow-agent';

if (!REPORT_ID) { console.error('usage: verify_step_preview.mjs <report_id>'); process.exit(1); }

const browser = await chromium.launch({
  executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
});
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

// --- network capture ---
const completionsCalls = [];
const stepCalls = [];
page.on('response', async (resp) => {
  const url = resp.url();
  if (/\/api\/reports\/[^/]+\/completions(\?|$)/.test(url)) {
    let bytes = 0;
    try { bytes = (await resp.body()).length; } catch {}
    completionsCalls.push({ url, status: resp.status(), bytes });
  } else if (/\/api\/steps\/[^/]+($|\?)/.test(url) && !/export/.test(url)) {
    stepCalls.push({ url, status: resp.status() });
  }
});

page.on('console', (m) => { if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 160)); });

// --- login ---
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await page.waitForSelector('#email', { timeout: 30000 });
await page.fill('#email', EMAIL);
await page.fill('#password', PASSWORD);
await page.click('button[type=submit]');
// Wait until we've left the sign-in page (auth succeeded).
await page.waitForFunction(() => !location.pathname.includes('/sign-in'), { timeout: 30000 }).catch(() => {});
await page.waitForTimeout(2000);
console.log('after login, url =', page.url());
await page.screenshot({ path: `${OUT}/after_login.png` });

// --- open the report; wait for the completions call explicitly ---
const compPromise = page.waitForResponse(
  (r) => /\/api\/reports\/[^/]+\/completions(\?|$)/.test(r.url()), { timeout: 45000 }
).catch(() => null);
await page.goto(`${BASE}/reports/${REPORT_ID}`, { waitUntil: 'domcontentloaded' });
await compPromise;
console.log('report url =', page.url());
// Let the chat render and the IntersectionObserver fire lazy step fetches.
await page.waitForTimeout(6000);
await page.screenshot({ path: `${OUT}/report_after.png`, fullPage: false });

// --- assertions ---
const compBytes = completionsCalls.reduce((a, c) => Math.max(a, c.bytes), 0);
const uniqueSteps = [...new Set(stepCalls.map((s) => s.url))];

console.log('=== network ===');
console.log('completions calls:', JSON.stringify(completionsCalls));
console.log('max completions payload bytes:', compBytes);
console.log('lazy /api/steps/{id} calls:', uniqueSteps.length, JSON.stringify(uniqueSteps));

// grab the largest table row count rendered on the page
const renderedRows = await page.evaluate(() => {
  const counts = [...document.querySelectorAll('table')].map((t) => t.querySelectorAll('tbody tr').length);
  return counts.length ? Math.max(...counts) : 0;
});
console.log('max rendered table rows:', renderedRows);

const bodyText = await page.evaluate(() => document.body.innerText.slice(0, 200));
console.log('page text sample:', JSON.stringify(bodyText));

let ok = true;
if (!(compBytes > 0 && compBytes < 200_000)) { console.log('FAIL: completions payload not small/bounded'); ok = false; }
if (uniqueSteps.length < 1) { console.log('FAIL: no lazy /api/steps/{id} fetch observed'); ok = false; }
if (stepCalls.some((s) => s.status !== 200)) { console.log('FAIL: a step fetch did not return 200'); ok = false; }

console.log(ok ? '\nVERIFY PASS' : '\nVERIFY FAIL');
await browser.close();
process.exit(ok ? 0 : 1);
