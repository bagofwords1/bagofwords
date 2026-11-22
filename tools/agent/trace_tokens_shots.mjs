// UI evidence for BOW-216 (trace tokens and models), driven as a signed-in user.
//
//   node tools/agent/trace_tokens_shots.mjs <out-dir> <prefix> <chat-report-id> <query>
//
// Captures, at deviceScaleFactor 2:
//   <prefix>-trace-strip.png       TraceModal turn strip (tokens + model; models row expanded when present)
//   <prefix>-step-timing.png       step-timing block of a tool step
//   <prefix>-diagnosis.png         diagnosis page with <query> applied
//   <prefix>-trace-he.png          the trace modal in Hebrew (RTL)
// Report ids come from tools/agent/trace_tokens_live.py. Credentials: BOW_EMAIL / BOW_PASSWORD.
import { createRequire } from 'node:module';
import { mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');

const [outDir, prefix, chatReport, query] = process.argv.slice(2);
if (!outDir || !prefix || !chatReport || !query) {
  console.error('usage: node trace_tokens_shots.mjs <out-dir> <prefix> <chat-report-id> <query>');
  process.exit(2);
}
const origin = process.env.BOW_BASE_URL_UI || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
mkdirSync(outDir, { recursive: true });

// Cloud sandboxes whose browser build differs from frontend's Playwright pin:
// BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium.
const browser = await chromium.launch({ executablePath: process.env.BOW_CHROMIUM_EXECUTABLE || undefined });

async function session(locale) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  await context.addInitScript((loc) => { try { localStorage.setItem('bow.locale', loc) } catch {} }, locale);
  const page = await context.newPage();
  await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.fill('#email', email);
  await page.fill('#password', password);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.startsWith('/users/sign-in'), { timeout: 60_000 });
  return { context, page };
}

const traceTitle = (locale) =>
  JSON.parse(readFileSync(new URL(`../../locales/${locale}.json`, import.meta.url), 'utf8')).reportView.viewAgentTrace;

async function openTrace(page, locale) {
  await page.goto(`${origin}/reports/${chatReport}`, { waitUntil: 'load' });
  // The button speaks the conversation's language (responseT), not always the UI's.
  await page.locator(`button[title="${traceTitle(locale)}"], button[title="${traceTitle('en')}"]`).first().click({ timeout: 60_000 });
  await page.locator('[data-testid="step-timing-bar"]').first().waitFor({ timeout: 60_000 });
  await page.waitForTimeout(800);
}

// The modal panel: the widest ancestor of the timeline that still fits the viewport.
async function shootModal(page, path) {
  const clip = await page.locator('[data-testid="step-timing-bar"]').first().evaluate((el) => {
    let best = el;
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      const r = n.getBoundingClientRect();
      if (r.width > 1000 && r.width < window.innerWidth && r.height < window.innerHeight) best = n;
    }
    const r = best.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  });
  await page.screenshot({ path, clip });
}

// English: strip + step timing
{
  const { context, page } = await session('en');
  await openTrace(page, 'en');
  const model = page.locator('[data-testid="turn-model"]');
  if (await model.count() && await model.isEnabled()) await model.click();
  await page.waitForTimeout(400);
  await shootModal(page, join(outDir, `${prefix}-trace-strip.png`));

  // A create_data step: the first timeline row whose title names it, else the first step.
  const rows = page.locator('button:has([data-testid="step-timing-bar"])');
  const tool = rows.filter({ hasText: /create_data|Create data|data/i }).first();
  await (await tool.count() ? tool : rows.first()).click();
  const detail = page.locator('[data-testid="step-timing-detail"]');
  await detail.waitFor({ timeout: 30_000 });
  await detail.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await shootModal(page, join(outDir, `${prefix}-step-timing.png`));

  await page.goto(`${origin}/monitoring/diagnosis?q=${encodeURIComponent(query)}`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.locator('[data-testid="run-row"]').first().waitFor({ timeout: 60_000 });
  await page.waitForTimeout(800);
  await page.screenshot({ path: join(outDir, `${prefix}-diagnosis.png`) });
  await context.close();
}

// Hebrew (RTL): the trace modal
{
  const { context, page } = await session('he');
  await openTrace(page, 'he');
  const model = page.locator('[data-testid="turn-model"]');
  if (await model.count() && await model.isEnabled()) await model.click();
  const rows = page.locator('button:has([data-testid="step-timing-bar"])');
  await rows.first().click();
  await page.waitForTimeout(600);
  await shootModal(page, join(outDir, `${prefix}-trace-he.png`));
  await context.close();
}

await browser.close();
console.log(`captured ${prefix} shots in ${outDir}`);
