import { test, expect } from '@playwright/test';

test('can view instructions page', async ({ page }) => {
  await page.goto('/instructions');
  await page.waitForLoadState('domcontentloaded');

  // Verify page heading
  await expect(page.getByRole('heading', { name: 'Instructions', exact: true }))
    .toBeVisible({ timeout: 10000 });

  // Verify page description
  await expect(page.getByText('Create and manage your instructions'))
    .toBeVisible();
});

