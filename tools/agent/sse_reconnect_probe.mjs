// SSE reconnect probe for /reports/[id] — see docs/feedback-loops/sse-reconnect.md.
// Scenario A (refresh): reload mid-stream, assert live streaming resumes.
// Scenario B (drop): sever TCP mid-stream via chaos_proxy.mjs, assert recovery.
//
// Run from frontend/ (for @playwright/test resolution), stack booted + seeded:
//   export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
//   REPORT_ID=<report-with-datasource> node ../tools/agent/sse_reconnect_probe.mjs <outdir> refresh
//   BASE=http://localhost:3001 REPORT_ID=... node ../tools/agent/sse_reconnect_probe.mjs <outdir> drop
// Scenario drop needs chaos_proxy.mjs running (pid in /tmp/bow-agent/chaos.pid).
import { chromium } from '@playwright/test';
import fs from 'fs';

const BASE = process.env.BASE || 'http://localhost:3000';
const OUT = process.argv[2] || '/tmp/bow-agent/evidence';
const SCENARIO = process.argv[3] || 'all';
const REPORT_ID = process.env.REPORT_ID || '87142904-5177-48e8-8f66-733c1461c2dc';

fs.mkdirSync(OUT, { recursive: true });

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

async function login(page) {
  await page.goto(`${BASE}/users/sign-in`);
  await page.fill('input[type="text"]', 'admin@example.com');
  await page.fill('input[type="password"]', 'Password123!');
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 });
  if (page.url().includes('/onboarding')) {
    const skip = page.getByRole('button', { name: 'Skip onboarding' });
    if (await skip.isVisible({ timeout: 10000 }).catch(() => false)) {
      await skip.click();
      await page.waitForURL((u) => !u.pathname.includes('/onboarding'), { timeout: 15000 });
    }
  }
  log('logged in ->', page.url());
}

async function startPrompt(page, text) {
  await page.goto(`${BASE}/reports/${REPORT_ID}`);
  await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
  const box = page.locator('.mention-input-field').first();
  await box.waitFor({ state: 'visible', timeout: 60000 });
  await page.waitForTimeout(2000);
  await box.click();
  await box.pressSequentially(text, { delay: 10 });
  const send = page.locator('button.w-7.h-7.rounded-full:not([disabled])').last();
  await send.waitFor({ state: 'visible', timeout: 10000 });
  await send.click();
  log('prompt submitted');
}

async function shot(page, name) {
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
  log('screenshot', name);
}

// Wait until streamed text is visibly growing (block content updates)
async function waitForStreamingText(page, timeoutMs = 30000) {
  const start = Date.now();
  let prev = -1;
  while (Date.now() - start < timeoutMs) {
    const len = await page.evaluate(() => document.body.innerText.length);
    if (prev >= 0 && len > prev + 10) return true;
    prev = len;
    await page.waitForTimeout(1500);
  }
  return false;
}

async function scenarioRefresh(browser) {
  log('=== Scenario A: refresh mid-stream ===');
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await login(page);
  await startPrompt(page, 'Write a very long, detailed 3000-word essay on the history of data visualization. No tools, just prose.');
  await page.waitForTimeout(12000);
  await shot(page, 'A1-streaming-live');

  // Track network: after reload, is there an SSE connection or repeated polling?
  const requests = [];
  page.on('request', (r) => {
    const u = r.url();
    if (u.includes('/completions')) requests.push(`${new Date().toISOString().slice(11, 23)} ${r.method()} ${u.replace(BASE, '')}`);
  });

  page.on('console', (m) => { if (m.type() === 'error') log('console.error:', m.text().slice(0, 200)); });
  page.on('pageerror', (e) => log('pageerror:', String(e).slice(0, 300)));
  await page.reload();
  await page.waitForTimeout(8000);
  await shot(page, 'A2-after-refresh');

  const lenAtA2 = await page.evaluate(() => document.body.innerText.length);
  await page.waitForTimeout(10000);
  await shot(page, 'A3-after-refresh-later');
  const lenAtA3 = await page.evaluate(() => document.body.innerText.length);
  log(`text growth after refresh: ${lenAtA2} -> ${lenAtA3} (${lenAtA3 > lenAtA2 + 100 ? 'LIVE STREAMING' : 'STALLED'})`);

  const banner = await page.locator('text=showing recent progress').count();
  log(`polling banner visible: ${banner > 0}`);
  log('completions requests after reload:\n  ' + requests.join('\n  '));
  fs.writeFileSync(`${OUT}/A-network.log`, requests.join('\n'));
  await ctx.close();
}

async function scenarioDrop(browser) {
  log('=== Scenario B: network drop mid-stream ===');
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await login(page);
  await startPrompt(page, 'Write a very long, detailed 3000-word essay on the history of cartography. No tools, just prose.');
  await page.waitForTimeout(10000);
  await shot(page, 'B1-streaming-live');

  log('severing all TCP connections via chaos proxy...');
  const { execSync } = await import('node:child_process');
  execSync('kill -USR1 $(cat /tmp/bow-agent/chaos.pid)');
  log('connections severed');
  await page.waitForTimeout(6000);
  await shot(page, 'B2-after-network-blip');
  const lenAtB2 = await page.evaluate(() => document.body.innerText.length);

  const errText = await page.locator('text=/error occurred during streaming|Stream was cancelled|Connection error/i').count();
  log(`error UI visible after blip: ${errText > 0}`);

  await page.waitForTimeout(12000);
  await shot(page, 'B3-after-blip-later');
  const lenAtB3 = await page.evaluate(() => document.body.innerText.length);
  log(`text growth after sever: ${lenAtB2} -> ${lenAtB3} (${lenAtB3 > lenAtB2 + 100 ? 'LIVE STREAMING' : 'STALLED'})`);
  await page.waitForTimeout(30000);
  await page.reload();
  await page.waitForTimeout(8000);
  await shot(page, 'B4-final-truth');
  await ctx.close();
}

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome', headless: true });
try {
  if (SCENARIO === 'refresh' || SCENARIO === 'all') await scenarioRefresh(browser);
  if (SCENARIO === 'drop' || SCENARIO === 'all') await scenarioDrop(browser);
} finally {
  await browser.close();
}
log('done');
