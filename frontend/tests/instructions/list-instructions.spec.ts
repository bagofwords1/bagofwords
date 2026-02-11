import { test, expect } from '../fixtures/feature-test';

test('can view instructions page', async ({ page }) => {
  await page.goto('/instructions');
  await page.waitForLoadState('networkidle');

  // Verify page heading (longer timeout for CI)
  await expect(page.getByRole('heading', { name: 'Instructions', exact: true }))
    .toBeVisible({ timeout: 15000 });

  // Verify page description
  await expect(page.getByText('Create and manage your instructions'))
    .toBeVisible({ timeout: 10000 });
});

