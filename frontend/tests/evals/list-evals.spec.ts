import { test, expect } from '@playwright/test';

test('can view evals page', async ({ page }) => {
  await page.goto('/evals');
  await page.waitForLoadState('domcontentloaded');

  // Verify metrics cards are present
  await expect(page.getByText('Total Test Cases'))
    .toBeVisible({ timeout: 10000 });
  await expect(page.getByText('Total Test Runs'))
    .toBeVisible();

  // Verify tabs are present
  await expect(page.getByRole('button', { name: 'Tests' }))
    .toBeVisible();
  await expect(page.getByRole('button', { name: 'Test Runs' }))
    .toBeVisible();
});

