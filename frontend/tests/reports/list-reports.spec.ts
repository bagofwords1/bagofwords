import { test, expect } from '@playwright/test'

test('can list reports', async ({ page }) => {
  await page.goto('/reports');

  // Use a more specific locator and add timeout
  await expect(page.getByRole('heading', { name: 'Reports' }))
    .toBeVisible({ timeout: 5000 });
});
