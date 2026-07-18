// Direct Playwright script (no test runner / project setup) to screenshot the
// session-event strips in light and dark mode against the running dev stack.
//
//   EVENTS_REPORT_ID=<id> node tests/reports/shoot-session-events.mjs
import { chromium } from 'playwright';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000';
const REPORT_ID = process.env.EVENTS_REPORT_ID;
const OUT = process.env.SHOT_DIR || 'tests/.artifacts';
if (!REPORT_ID) { console.error('set EVENTS_REPORT_ID'); process.exit(1); }

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const ctx = await browser.newContext({ viewport: { width: 900, height: 1300 } });
const page = await ctx.newPage();

await page.goto(`${BASE}/users/sign-in`);
await page.waitForSelector('input[type="text"]', { timeout: 30000 });
await page.fill('input[type="text"]', 'admin@example.com');
await page.fill('input[type="password"]', 'Password123!');
await page.click('button[type="submit"]');
await page.waitForTimeout(3000);

await page.goto(`${BASE}/reports/${REPORT_ID}`, { waitUntil: 'domcontentloaded' });
await page.waitForSelector('li[data-message-id]', { timeout: 30000 });
await page.waitForTimeout(2500);

async function setTheme(mode) {
  await page.emulateMedia({ colorScheme: mode });
  await page.evaluate((m) => {
    document.documentElement.classList.toggle('dark', m === 'dark');
    document.documentElement.setAttribute('data-theme', m);
    document.documentElement.style.colorScheme = m;
  }, mode);
  await page.waitForTimeout(600);
}

await setTheme('light');
await page.screenshot({ path: `${OUT}/session-events-light.png`, fullPage: true });
console.log('wrote light');

await setTheme('dark');
await page.screenshot({ path: `${OUT}/session-events-dark.png`, fullPage: true });
console.log('wrote dark');

const body = await page.textContent('body');
for (const needle of ['uploaded targets_2025.xlsx', 'shared "Revenue vs Targets"', "changed the agent's scope"]) {
  console.log((body.includes(needle) ? 'OK   ' : 'MISS ') + needle);
}

await browser.close();
