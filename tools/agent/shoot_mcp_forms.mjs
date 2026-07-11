import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const BASE = 'http://localhost:3000';
const OUT = '../media/pr';
const CHROME = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({ executablePath: CHROME });
const ctx = await browser.newContext({ viewport: { width: 1360, height: 1040 } });
const page = await ctx.newPage();
page.setDefaultTimeout(60000);
page.setDefaultNavigationTimeout(60000);

// --- login + skip onboarding ---
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await page.locator('input[type="text"]').first().fill('admin@example.com');
await page.locator('input[type="password"]').first().fill('Password123!');
await page.locator('button[type="submit"]').first().click();
await page.waitForTimeout(3000);
if (page.url().includes('/onboarding')) {
  await page.getByRole('button', { name: /skip onboarding/i }).first().click().catch(() => {});
  await page.waitForTimeout(3000);
}

async function openCatalog() {
  await page.goto(`${BASE}/agents/new`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  const search = page.getByPlaceholder(/search data sources/i).first();
  if (!(await search.isVisible().catch(() => false))) {
    await page.getByRole('button', { name: /create new connection/i }).first().click().catch(() => {});
    await page.waitForTimeout(1200);
  }
  await search.waitFor({ timeout: 20000 });
  await page.waitForTimeout(1000);
}
async function pick(term, tileName) {
  const s = page.getByPlaceholder(/search data sources/i).first();
  await s.fill('');
  await s.fill(term);
  await page.waitForTimeout(900);
  await page.getByRole('button', { name: tileName, exact: true }).first().click();
  await page.waitForTimeout(1500);
}

// === X — OAuth (admin app): clean form, endpoints collapsed under Advanced ===
await openCatalog();
await pick('x', 'X');
await page.locator('select:has(option[value="oauth_app"])').selectOption('oauth_app');
await page.waitForTimeout(800);
await page.screenshot({ path: `${OUT}/mcp-x-oauth-app-prefilled.png` });
console.log('shot: mcp-x-oauth-app-prefilled.png');

// Advanced expanded — prefilled provider endpoints (server URL, authorize/token, scopes).
await page.getByRole('button', { name: /advanced/i }).first().click();
await page.waitForTimeout(600);
await page.screenshot({ path: `${OUT}/mcp-x-oauth-advanced.png` });
console.log('shot: mcp-x-oauth-advanced.png');
await page.getByRole('button', { name: /advanced/i }).first().click();
await page.waitForTimeout(300);

// === X — bearer mode: gated dropdown (no DCR) + shared-token note ===
await page.locator('select:has(option[value="oauth_app"])').selectOption('bearer');
await page.waitForTimeout(600);
await page.screenshot({ path: `${OUT}/mcp-x-bearer.png` });
console.log('shot: mcp-x-bearer.png');

// === Atlassian (DCR): admin-voiced banner + gated dropdown ===
await openCatalog();
await pick('jira', 'Jira / Atlassian');
await page.screenshot({ path: `${OUT}/mcp-atlassian-dcr.png` });
console.log('shot: mcp-atlassian-dcr.png');

await ctx.close();
await browser.close();
console.log('done');
