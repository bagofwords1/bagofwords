import { test, expect } from '@playwright/test';

test('can view monitoring page', async ({ page }) => {
  await page.goto('/monitoring');
  await page.waitForLoadState('networkidle');

  // Verify page heading
  await expect(page.getByRole('heading', { name: 'Monitoring', exact: true }))
    .toBeVisible({ timeout: 10000 });

  // Verify navigation tabs are present
  await expect(page.getByText('Explore'))
    .toBeVisible();
});

