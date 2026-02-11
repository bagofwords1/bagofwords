import { test, expect } from '@playwright/test';

test('can view queries page', async ({ page }) => {
  await page.goto('/queries');
  await page.waitForLoadState('networkidle');

  // Verify page heading (longer timeout for CI)
  await expect(page.getByRole('heading', { name: 'Queries', exact: true }))
    .toBeVisible({ timeout: 15000 });

  // Verify filter tabs are present
  await expect(page.getByRole('button', { name: 'Published' }))
    .toBeVisible({ timeout: 10000 });

  // Verify search input is present
  await expect(page.getByPlaceholder('Search entities'))
    .toBeVisible({ timeout: 10000 });
});

