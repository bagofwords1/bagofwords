import { test, expect } from '../fixtures/feature-test';

test('can view evals page', async ({ page }) => {
  await page.goto('/evals');
  await page.waitForLoadState('networkidle');

  // On a fresh org with no test cases or runs, /evals renders the
  // full-page empty state (metric cards / tabs / table are intentionally
  // hidden until there is data).
  await expect(page.getByRole('heading', { name: 'No tests found' }))
    .toBeVisible({ timeout: 15000 });
  await expect(page.getByRole('button', { name: 'Add New Test' }).first())
    .toBeVisible({ timeout: 10000 });
});
