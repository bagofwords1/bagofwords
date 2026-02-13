import { test as base, Page } from '@playwright/test';

// Dismiss onboarding if the page was redirected to it
async function dismissOnboardingIfNeeded(page: Page) {
  await page.goto('/');
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(3000);

  if (page.url().includes('/onboarding')) {
    const skipButton = page.getByRole('button', { name: 'Skip onboarding' });
    if (await skipButton.isVisible({ timeout: 10000 }).catch(() => false)) {
      await skipButton.click();
      await page.waitForURL(
        (url) => !url.pathname.includes('/onboarding'),
        { timeout: 15000 }
      );
    }
  }
}

// Extended test with role-specific page fixtures
export const test = base.extend<{
  adminPage: Page;
  memberPage: Page;
}>({
  // Admin page - uses admin auth state
  adminPage: async ({ browser }, use) => {
    const context = await browser.newContext({
      storageState: 'tests/config/admin.json',
    });
    const page = await context.newPage();
    // Ensure onboarding is dismissed for admin (only admins get redirected)
    await dismissOnboardingIfNeeded(page);
    await use(page);
    await context.close();
  },

  // Member page - uses member auth state
  memberPage: async ({ browser }, use) => {
    const context = await browser.newContext({
      storageState: 'tests/config/member.json',
    });
    const page = await context.newPage();
    await use(page);
    await context.close();
  },
});

export { expect } from '@playwright/test';

