import { test, expect } from '@playwright/test';

test('can view queries page', async ({ page }) => {
  await page.goto('/queries');
  await page.waitForLoadState('domcontentloaded');

  // Verify page heading
  await expect(page.getByRole('heading', { name: 'Queries', exact: true }))
    .toBeVisible({ timeout: 10000 });

  // Verify filter tabs are present
  await expect(page.getByRole('button', { name: 'Published' }))
    .toBeVisible();

  // Verify search input is present
  await expect(page.getByPlaceholder('Search entities'))
    .toBeVisible();
});

