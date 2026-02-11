import { test as base, expect } from '@playwright/test';

/**
 * Extended test fixture for feature tests that ensures onboarding is dismissed
 * before each test runs. This prevents test failures caused by the onboarding
 * middleware redirecting to /onboarding.
 */
export const test = base.extend({
  page: async ({ page }, use) => {
    // Navigate to home to check if onboarding redirect is active
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(3000);

    // If redirected to onboarding, dismiss it
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

    await use(page);
  },
});

export { expect } from '@playwright/test';
