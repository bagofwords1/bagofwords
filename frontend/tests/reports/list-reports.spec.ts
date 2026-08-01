import { test, expect } from '../fixtures/feature-test'

test('can list reports', async ({ page }) => {
  await page.goto('/reports', { waitUntil: 'commit' });

  // Use exact match for the main Reports heading (h1) - longer timeout for CI
  await expect(page.getByRole('heading', { name: 'Reports', exact: true }))
    .toBeVisible({ timeout: 15000 });
});
