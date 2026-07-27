// Log into BOW via the UI, then screenshot a page (authenticated).
//
//   cd frontend
//   node ../tools/agent/login_and_capture.mjs <path> <out.png> [--wait ms] [--full]
//
// Env: BOW_EMAIL, BOW_PASSWORD, BOW_ORIGIN (default http://localhost:3000)
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

const [path, out, ...rest] = process.argv.slice(2);
if (!path || !out) {
  console.error('usage: node login_and_capture.mjs <path> <out.png> [--wait ms] [--full]');
  process.exit(2);
}
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const email = process.env.BOW_EMAIL || 'admin@example.com';
const password = process.env.BOW_PASSWORD || 'Password123!';
const full = rest.includes('--full');
const waitIdx = rest.indexOf('--wait');
const extraWait = waitIdx >= 0 ? Number(rest[waitIdx + 1] || 0) : 0;

mkdirSync(dirname(out), { recursive: true });
// Sandbox chromium may be a different build than the pinned @playwright/test
// expects — point at the pre-provisioned binary instead of downloading.
const execPath = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const browser = await chromium.launch({ executablePath: execPath });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();

// Sign in.
await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', email);
await page.fill('#password', password);
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.click('button[type=submit]'),
]);
await page.waitForTimeout(1500);

// Fresh orgs land on the onboarding wizard, which gates app pages. Skip it.
try {
  if (page.url().includes('/onboarding')) {
    const skip = page.getByText('Skip onboarding', { exact: false });
    await skip.click({ timeout: 4000 });
    await page.waitForTimeout(1500);
  }
} catch {}

// Navigate to the target page.
await page.goto(`${origin}${path}`, { waitUntil: 'networkidle' }).catch(() => page.goto(`${origin}${path}`, { waitUntil: 'load' }));
// Optional: click an element by text after navigating (e.g. a tab).
const clickIdx = rest.indexOf('--click');
if (clickIdx >= 0) {
  const label = rest[clickIdx + 1];
  try {
    // Prefer an exact button/tab match so we don't hit a stat card that
    // merely contains the label as a substring (e.g. "Total Test Runs").
    const byRole = page.getByRole('button', { name: label, exact: true });
    if (await byRole.count()) await byRole.first().click({ timeout: 5000 });
    else await page.getByText(label, { exact: true }).first().click({ timeout: 5000 });
    await page.waitForTimeout(1500);
  } catch (e) { console.error(`click "${label}" failed: ${e.message}`); }
}
if (extraWait) await page.waitForTimeout(extraWait);
await page.waitForTimeout(1000); // settle

await page.screenshot({ path: out, fullPage: full });
console.log(`captured ${out} (url=${page.url()})`);
await context.close();
await browser.close();
