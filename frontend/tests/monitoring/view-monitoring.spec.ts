import { test, expect } from '@playwright/test';

test('can view monitoring page', async ({ page }) => {
  await page.goto('/monitoring');
  await page.waitForLoadState('domcontentloaded');

  // Verify page heading (longer timeout for CI)
  await expect(page.getByRole('heading', { name: 'Monitoring', exact: true }))
    .toBeVisible({ timeout: 30000 });

  // Verify navigation tabs are present
  await expect(page.getByText('Explore'))
    .toBeVisible({ timeout: 10000 });
});

