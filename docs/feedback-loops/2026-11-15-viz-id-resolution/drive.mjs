// Loop B driver: send one chat message through the real UI and wait for the run.
//
//   node drive.mjs <setup.json> <report_id|new> "<message>" <out_prefix>
//
// Prints {report_id, dashboard_rows_rendered}. Writes <out_prefix>.png, and
// <out_prefix>.dashboard.png when a dashboard was made, and
// <out_prefix>.http.jsonl (every /api/ response).
import fs from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const [setupPath, reportArg, message, out] = process.argv.slice(2);
const setup = JSON.parse(fs.readFileSync(setupPath, 'utf8'));
const FRONT = process.env.BOW_FRONT_URL || 'http://localhost:3000';
const API = process.env.BOW_BASE_URL || 'http://localhost:8000';
const headers = { Authorization: `Bearer ${setup.token}`, 'X-Organization-Id': setup.org_id, 'Content-Type': 'application/json' };

let reportId = reportArg;
if (reportArg === 'new') {
  const resp = await fetch(`${API}/api/reports`, {
    method: 'POST', headers,
    body: JSON.stringify({ title: 'Viz id resolution', files: [], data_sources: setup.data_sources.map((d) => d.id) }),
  });
  if (!resp.ok) throw new Error(`create report: ${resp.status} ${await resp.text()}`);
  reportId = (await resp.json()).id;
}

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1280, height: 1100 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
const http = fs.createWriteStream(`${out}.http.jsonl`);
page.on('response', (r) => { if (r.url().includes('/api/')) http.write(JSON.stringify({ s: r.status(), m: r.request().method(), u: r.url() }) + '\n'); });

await page.goto(`${FRONT}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await page.locator('#email').waitFor({ timeout: 120000 });
await page.fill('#email', setup.email);
await page.fill('#password', setup.password);
await page.click('button[type=submit]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 120000 });

await page.goto(`${FRONT}/reports/${reportId}`, { waitUntil: 'domcontentloaded' });
const input = page.locator('[contenteditable="true"]').first();
await input.waitFor({ timeout: 180000 });
await page.waitForTimeout(3000);
await input.click();
await page.keyboard.type(message, { delay: 5 });
await page.waitForTimeout(500);
await page.locator('button[class*="rounded-full"]').last().click();

// The run is over when the stop button has appeared and gone again.
const stop = page.locator('[data-testid="stop-button"]');
await stop.waitFor({ state: 'visible', timeout: 60000 }).catch(() => {});
await stop.waitFor({ state: 'hidden', timeout: 600000 });
await page.waitForTimeout(4000);
await page.screenshot({ path: `${out}.png`, fullPage: true });
// When a dashboard was made, open it and wait for it to render the rows.
let rowsRendered = null;
// (Without one the same tab lists the report's charts, so only look when the
// chat shows that create_artifact succeeded.)
const created = await page.getByText('Dashboard Created').first().isVisible().catch(() => false);
const tab = page.getByText('Dashboard', { exact: true }).last();
if (created && await tab.isVisible().catch(() => false)) {
  await tab.click();
  rowsRendered = false;
  for (let i = 0; i < 45 && !rowsRendered; i++) {
    await page.waitForTimeout(2000);
    for (const f of page.frames()) {
      const text = await f.evaluate(() => (document.body ? document.body.innerText : '')).catch(() => '');
      if (text.includes('Rock') && text.includes('826.65')) rowsRendered = true;
    }
  }
  await page.screenshot({ path: `${out}.dashboard.png`, fullPage: true });
}
await browser.close();
http.end();
console.log(JSON.stringify({ report_id: reportId, dashboard_rows_rendered: rowsRendered }));
