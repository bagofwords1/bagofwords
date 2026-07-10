// Screenshot the connection-tool call titles in the real report UI.
// Logs in through the sign-in form, opens the seeded demo report, waits for the
// tool blocks to render, and captures a full-page screenshot.
//
//   cd frontend
//   node ../tools/agent/shoot_tool_titles.mjs <reportUrl> <out.png> [email] [password]
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { dirname as _d } from 'node:path';

const [reportUrl, out, email = 'admin@example.com', password = 'Password123!'] = process.argv.slice(2);
if (!reportUrl || !out) {
  console.error('usage: node shoot_tool_titles.mjs <reportUrl> <out.png> [email] [password]');
  process.exit(2);
}
mkdirSync(_d(out), { recursive: true });

const browser = await chromium.launch({
  executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
});
const context = await browser.newContext({ viewport: { width: 1280, height: 1200 } });
const page = await context.newPage();

// --- login ---------------------------------------------------------------
await page.goto('http://localhost:3000/users/sign-in', { waitUntil: 'load' });
await page.waitForSelector('#email', { timeout: 30000 });
await page.fill('#email', email);
await page.fill('#password', password);
await Promise.all([
  page.waitForNavigation({ waitUntil: 'load' }).catch(() => {}),
  page.click('button[type="submit"]'),
]);
await page.waitForTimeout(2500);

// New orgs land on an onboarding modal that blocks the app — skip it.
try {
  const skip = page.getByText('Skip onboarding', { exact: false });
  if (await skip.isVisible({ timeout: 4000 })) {
    await skip.click();
    await page.waitForTimeout(1000);
  }
} catch { /* no onboarding modal — fine */ }

// --- open the report -----------------------------------------------------
await page.goto(reportUrl, { waitUntil: 'networkidle' }).catch(() => page.goto(reportUrl, { waitUntil: 'load' }));

// Wait until at least one of our titles is on the page.
const needle = 'Searching Notion for churned customers';
try {
  await page.waitForFunction(
    (t) => document.body && document.body.innerText.includes(t),
    needle,
    { timeout: 20000 },
  );
} catch {
  console.error('WARN: title text not found within timeout; capturing anyway');
}
await page.waitForTimeout(1200); // fonts/shimmer settle

await page.screenshot({ path: out, fullPage: true });
console.log(`captured ${out}`);

// Report what titles are visible, for the run log.
const found = await page.evaluate(() => {
  const wanted = [
    'Finding available Notion tools',
    'Searching Notion for churned customers',
    'Reading the Churn Playbook page',
    'Reading the pricing page',
  ];
  const text = document.body.innerText;
  return wanted.filter((w) => text.includes(w));
});
console.log('visible titles:', JSON.stringify(found));

await context.close();
await browser.close();
process.exit(found.length >= 3 ? 0 : 1);
