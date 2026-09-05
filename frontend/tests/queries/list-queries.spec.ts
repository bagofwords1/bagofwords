import { test, expect } from '../fixtures/feature-test';

// Queries live in the /agents knowledge tree, under the agents they read.
// The old standalone /queries page is a redirect, kept for existing links.
test('the queries catalog opens in the agents tree', async ({ page }) => {
  await page.goto('/agents', { waitUntil: 'commit' });

  await expect(page.getByRole('heading', { name: 'Agents', exact: true }))
    .toBeVisible({ timeout: 15000 });
});

test('/queries redirects into the tree', async ({ page }) => {
  await page.goto('/queries', { waitUntil: 'commit' });

  await expect(page).toHaveURL(/\/agents$/, { timeout: 15000 });
  await expect(page.getByRole('heading', { name: 'Agents', exact: true }))
    .toBeVisible({ timeout: 15000 });
});
