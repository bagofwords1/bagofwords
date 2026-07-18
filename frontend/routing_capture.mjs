// Auth'd screenshot helper for the auto-routing UI evidence.
// Run from frontend/: node routing_capture.mjs <path> <out.png> [--wait ms] [--click "Label"] [--full]
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

const [path, out, ...rest] = process.argv.slice(2);
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const full = rest.includes('--full');
const waitIdx = rest.indexOf('--wait');
const extraWait = waitIdx >= 0 ? Number(rest[waitIdx + 1] || 0) : 0;
const clickIdx = rest.indexOf('--click');
const clickLabel = clickIdx >= 0 ? rest[clickIdx + 1] : null;

mkdirSync(dirname(out), { recursive: true });
const execPath = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const browser = await chromium.launch({ executablePath: execPath });
const context = await browser.newContext({ viewport: { width: 1500, height: 950 } });
const page = await context.newPage();

await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', email).catch(() => {});
await page.fill('#password', password).catch(() => {});
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.click('button[type=submit]').catch(() => {}),
]);
await page.waitForTimeout(1500);
try {
  if (page.url().includes('/onboarding')) {
    await page.getByText('Skip', { exact: false }).first().click({ timeout: 4000 });
    await page.waitForTimeout(1200);
  }
} catch {}

await page.goto(`${origin}${path}`, { waitUntil: 'networkidle' }).catch(() => page.goto(`${origin}${path}`, { waitUntil: 'load' }));
await page.waitForTimeout(1500);
if (clickLabel) {
  try {
    const el = page.getByText(clickLabel, { exact: false }).first();
    await el.click({ timeout: 6000 });
    await page.waitForTimeout(1500);
  } catch (e) { console.error(`click "${clickLabel}" failed: ${e.message}`); }
}
if (extraWait) await page.waitForTimeout(extraWait);
await page.waitForTimeout(800);
await page.screenshot({ path: out, fullPage: full });
console.log(`captured ${out} (url=${page.url()})`);
await context.close();
await browser.close();
