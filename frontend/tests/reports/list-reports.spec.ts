import { test, expect } from '@playwright/test'

test('can list reports', async ({ page }) => {
  await page.goto('/reports');
  await page.waitForLoadState('networkidle');

  // Use exact match for the main Reports heading (h1) - longer timeout for CI
  await expect(page.getByRole('heading', { name: 'Reports', exact: true }))
    .toBeVisible({ timeout: 15000 });
});
