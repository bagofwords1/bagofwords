import { test, expect } from '@playwright/test';

test('can view evals page', async ({ page }) => {
  await page.goto('/evals');
  await page.waitForLoadState('networkidle');

  // Verify metrics cards are present (longer timeout for CI)
  await expect(page.getByText('Total Test Cases'))
    .toBeVisible({ timeout: 15000 });
  await expect(page.getByText('Total Test Runs'))
    .toBeVisible({ timeout: 10000 });

  // Verify tabs are present
  await expect(page.getByRole('button', { name: 'Tests' }))
    .toBeVisible({ timeout: 10000 });
  await expect(page.getByRole('button', { name: 'Test Runs' }))
    .toBeVisible({ timeout: 10000 });
});

