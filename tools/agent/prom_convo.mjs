// Drive a real BOW conversation over the Prometheus data source (Haiku).
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = '../media/prometheus';
const BASE = 'http://localhost:3000';
const PROMPT = process.argv[2] || 'Which scrape targets are currently down? List them with their job and instance.';
const TAG = process.argv[3] || 'convo';
const WAIT = parseInt(process.argv[4] || '150', 10);
mkdirSync(OUT, { recursive: true });

const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);

// login
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.getByPlaceholder(/email/i).fill('admin@example.com');
await page.getByPlaceholder(/password/i).fill('Password123!');
await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.locator('button[type=submit]').first().click()]);
await page.waitForTimeout(2500);

await page.goto(`${BASE}/`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(3000);

// Select the Prometheus data source if a selector chip is present.
try {
  await page.getByText(/select data|data source|connect data/i).first().click({ timeout: 3000 });
  await page.waitForTimeout(500);
  await page.getByText(/prod prometheus|prometheus/i).first().click({ timeout: 3000 });
  await page.keyboard.press('Escape').catch(() => {});
} catch { /* likely auto-selected (only one source) */ }

// Type into the contenteditable prompt box.
const box = page.locator('[contenteditable=true]').first();
await box.click();
await page.keyboard.type(PROMPT, { delay: 8 });
await page.waitForTimeout(600);
await page.screenshot({ path: `${OUT}/10-${TAG}-typed.png` });

// Submit: the round send button (w-7 h-7 rounded-full with the arrow icon).
const sendBtn = page.locator('button.rounded-full').last();
console.log('send button enabled:', await sendBtn.isEnabled().catch(() => 'n/a'));
await sendBtn.click({ timeout: 8000 }).catch(async () => { await box.click(); await page.keyboard.press('Enter'); });
await page.waitForTimeout(3000);
console.log('url after submit:', page.url());
await page.screenshot({ path: `${OUT}/11-${TAG}-submitted.png` });

// Wait for the agent (Haiku: plan -> code -> observe -> answer). Periodic shots.
for (let i = 0; i < Math.ceil(WAIT / 6); i++) {
  await page.waitForTimeout(6000);
  await page.screenshot({ path: `${OUT}/12-${TAG}-progress.png` });
}
console.log('final url:', page.url());
await page.screenshot({ path: `${OUT}/13-${TAG}-result.png` });
await page.screenshot({ path: `${OUT}/14-${TAG}-result-full.png`, fullPage: true });
await ctx.close(); await b.close(); console.log('done');
