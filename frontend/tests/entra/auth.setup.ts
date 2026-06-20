import { test as setup, expect, Page } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const AUTH_DIR = path.join(__dirname, '.auth');

const DEMO_EMAIL = process.env.BOW_OAUTH_TEST_DEMO1_EMAIL || '';
const DEMO_PASSWORD = process.env.BOW_OAUTH_TEST_DEMO1_PASSWORD || '';

setup.skip(!DEMO_EMAIL || !DEMO_PASSWORD, 'BOW_OAUTH_TEST_DEMO1_* env vars not set');

async function microsoftLogin(page: Page, email: string, password: string) {
  await page.waitForSelector('input[type="email"], input[name="loginfmt"]', { timeout: 60000 });
  await page.fill('input[type="email"], input[name="loginfmt"]', email);
  await page.click('input[type="submit"], #idSIButton9');
  await page.waitForSelector('input[type="password"], input[name="passwd"]', { timeout: 60000 });
  await page.fill('input[type="password"], input[name="passwd"]', password);
  await page.click('input[type="submit"], #idSIButton9');
  // "Stay signed in?" — answer "No" if shown
  try {
    await page.waitForSelector('#idBtn_Back, #idSIButton9', { timeout: 15000 });
    const back = page.locator('#idBtn_Back');
    if (await back.count()) await back.click();
  } catch (_) {}
}

setup('authenticate demo1 via Entra and persist session', async ({ page }) => {
  fs.mkdirSync(AUTH_DIR, { recursive: true });

  await page.goto('/users/sign-in', { waitUntil: 'load' });
  const oidcButton = page.getByRole('button', { name: /entra|sign in with/i });
  await expect(oidcButton.first()).toBeVisible({ timeout: 30000 });
  await Promise.all([
    page.waitForURL(/login\.microsoftonline\.com/, { timeout: 60000 }),
    oidcButton.first().click(),
  ]);

  await microsoftLogin(page, DEMO_EMAIL, DEMO_PASSWORD);

  // Capture the BOW session JWT from the OAuth landing URL.
  await page.waitForURL(/access_token=/, { timeout: 90000 });
  const url = new URL(page.url());
  const token = url.searchParams.get('access_token') || '';
  expect(token, 'expected an access_token in the OAuth landing URL').toBeTruthy();

  // Let the SPA consume the token and route away from sign-in.
  await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 60000 });
  await page.waitForLoadState('domcontentloaded');

  await page.context().storageState({ path: path.join(AUTH_DIR, 'demo1.json') });
  fs.writeFileSync(path.join(AUTH_DIR, 'token.json'), JSON.stringify({ token, email: DEMO_EMAIL }, null, 2));
  console.log('[setup] session + token persisted for', DEMO_EMAIL);
});
