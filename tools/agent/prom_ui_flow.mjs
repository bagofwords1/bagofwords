// Drives the BOW UI to exercise the Prometheus connector and capture evidence.
// Run from frontend/:  node ../tools/agent/prom_ui_flow.mjs <outdir>
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.argv[2] || '../media/prometheus';
const BASE = 'http://localhost:3000';
const EMAIL = 'admin@example.com';
const PASSWORD = 'Password123!';
const PROM_URL = 'http://localhost:9090';
mkdirSync(OUT, { recursive: true });

const shot = async (page, name) => {
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
  console.log('shot', name);
};

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);

// 1. Sign in
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.getByPlaceholder(/email/i).fill(EMAIL);
await page.getByPlaceholder(/password/i).fill(PASSWORD);
await shot(page, '00-sign-in');
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2000);
console.log('after login url:', page.url());

// 2. Data source picker grid
await page.goto(`${BASE}/onboarding/data`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(1500);
await shot(page, '01-datasource-grid');

// 3. Select Prometheus tile
const tile = page.getByRole('button', { name: /prometheus/i }).first();
await tile.click({ timeout: 15000 }).catch(async () => {
  await page.getByText(/prometheus/i).first().click();
});
await page.waitForTimeout(1200);
await shot(page, '02-connect-form');

// 4. Fill the connect form (base_url) and give it a name
const nameField = page.getByPlaceholder(/name/i).first();
if (await nameField.count()) await nameField.fill('Prod Prometheus').catch(() => {});
// base_url field: match by label/placeholder "Base URL"
const urlField = page.locator('input').filter({ hasNot: page.locator('[type=password]') });
// Prefer an explicit match
const baseUrl = page.getByPlaceholder(/base url|http/i).first();
if (await baseUrl.count()) {
  await baseUrl.fill(PROM_URL);
} else {
  // fallback: first empty text input after the name
  await page.locator('input[type=text]').nth(1).fill(PROM_URL).catch(() => {});
}
await page.waitForTimeout(400);
await shot(page, '03-connect-form-filled');

// 5. Submit / Test connection
const testBtn = page.getByRole('button', { name: /test connection|test|connect|save|add/i }).first();
await testBtn.click({ timeout: 15000 }).catch(() => {});
await page.waitForTimeout(4000);
await shot(page, '04-after-submit');

// 6. Whatever page we land on (tables/schema or list), capture full page too
await page.screenshot({ path: `${OUT}/05-result-full.png`, fullPage: true });
console.log('final url:', page.url());

await ctx.close();
await browser.close();
console.log('done');
