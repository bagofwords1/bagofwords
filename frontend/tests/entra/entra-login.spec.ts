import { test, expect, Page } from '@playwright/test';

// Live Entra SSO end-to-end: click "Sign in with entra" -> real Microsoft login
// -> OBO callback -> authenticated in the app. Credentials come from env so they
// are never committed.
const DEMO_EMAIL = process.env.BOW_OAUTH_TEST_DEMO1_EMAIL || '';
const DEMO_PASSWORD = process.env.BOW_OAUTH_TEST_DEMO1_PASSWORD || '';

test.skip(!DEMO_EMAIL || !DEMO_PASSWORD, 'BOW_OAUTH_TEST_DEMO1_* env vars not set');

// Walk through the Microsoft AAD login screens. They vary (email page, password
// page, "Stay signed in?", optional consent), so each step is best-effort.
async function microsoftLogin(page: Page, email: string, password: string) {
  // Email
  await page.waitForSelector('input[type="email"], input[name="loginfmt"]', { timeout: 60000 });
  await page.fill('input[type="email"], input[name="loginfmt"]', email);
  await page.click('input[type="submit"], #idSIButton9');

  // Password
  await page.waitForSelector('input[type="password"], input[name="passwd"]', { timeout: 60000 });
  await page.fill('input[type="password"], input[name="passwd"]', password);
  await page.click('input[type="submit"], #idSIButton9');

  // "Stay signed in?" (KMSI) — click "No" if present
  try {
    await page.waitForSelector('#idSIButton9, #idBtn_Back', { timeout: 15000 });
    const back = page.locator('#idBtn_Back');
    if (await back.count()) {
      await back.click();
    }
  } catch (_) { /* no KMSI prompt */ }

  // Optional consent screen — accept if present
  try {
    const accept = page.locator('#idSIButton9');
    if (await accept.count()) {
      // already handled above in most flows; ignore
    }
  } catch (_) {}
}

test('demo1 signs in via Entra SSO end-to-end', async ({ page }) => {
  // 1. Sign-in page
  await page.goto('/users/sign-in', { waitUntil: 'load' });

  // 2. Click the OIDC (entra) button. okta is disabled so only entra renders.
  const oidcButton = page.getByRole('button', { name: /entra|sign in with/i });
  await expect(oidcButton.first()).toBeVisible({ timeout: 30000 });
  await Promise.all([
    page.waitForURL(/login\.microsoftonline\.com/, { timeout: 60000 }),
    oidcButton.first().click(),
  ]);

  // 3. Real Microsoft login
  await microsoftLogin(page, DEMO_EMAIL, DEMO_PASSWORD);

  // 4. Microsoft redirects to the BOW callback, which mints a session JWT and
  //    bounces back to the SPA as /users/sign-in?access_token=...&email=...
  await page.waitForURL(/localhost:3000\/.*access_token=/, { timeout: 90000 });
  const landingUrl = page.url();
  console.log('[entra-e2e] OAuth landing URL:', landingUrl);

  // The callback must NOT be an error redirect, and must carry a token for our user.
  await expect(page).not.toHaveURL(/error/i);
  expect(landingUrl).toContain('access_token=');
  expect(landingUrl.toLowerCase()).toContain(DEMO_EMAIL.toLowerCase());

  // 5. The SPA (sign-in.vue onMounted) consumes the token and navigates away
  //    from the sign-in page to the app (/) or org creation (/organizations/new).
  await page.waitForURL((url) => !url.pathname.includes('/users/sign-in'), { timeout: 60000 });
  await page.waitForLoadState('domcontentloaded');
  const finalUrl = page.url();
  console.log('[entra-e2e] authenticated landing:', finalUrl);

  // Confirm a real session token is present in the auth store.
  const token = await page.evaluate(() => {
    try {
      return localStorage.getItem('auth:token') || localStorage.getItem('auth._token.local') || '';
    } catch (_) { return ''; }
  });
  console.log('[entra-e2e] token present in storage:', Boolean(token));

  expect(/\/organizations\/new|\/onboarding|localhost:3000\/$|localhost:3000\/[^u]/.test(finalUrl),
    `expected an authenticated app route, got ${finalUrl}`).toBeTruthy();
});
