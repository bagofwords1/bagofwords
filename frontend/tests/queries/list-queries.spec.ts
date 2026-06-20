import { test, expect } from '../fixtures/feature-test';

test('can view queries page', async ({ page }) => {
  await page.goto('/queries');
  await page.waitForLoadState('networkidle');

  // Page heading is always visible.
  await expect(page.getByRole('heading', { name: 'Queries', exact: true }))
    .toBeVisible({ timeout: 15000 });

  // On a fresh org with no entities, /queries renders the full-page empty
  // state (filter tabs / search input are intentionally hidden until there
  // is data).
  await expect(page.getByRole('heading', { name: 'Nothing published yet' }))
    .toBeVisible({ timeout: 10000 });
});
