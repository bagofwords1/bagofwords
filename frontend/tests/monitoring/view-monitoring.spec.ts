import { test, expect } from '../fixtures/feature-test';

test('can view monitoring page', async ({ page }) => {
  // 'load'/'networkidle' are unreliable on CI for this data-heavy page; commit
  // the navigation and wait on concrete content instead.
  await page.goto('/monitoring', { waitUntil: 'commit' });

  // Verify page heading (longer timeout for CI)
  await expect(page.getByRole('heading', { name: 'Monitoring', exact: true }))
    .toBeVisible({ timeout: 30000 });

  // Verify navigation tabs are present
  await expect(page.getByText('Explore'))
    .toBeVisible({ timeout: 15000 });
});

