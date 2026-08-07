import { test, expect } from '../fixtures/feature-test';

test('/evals redirects into the Agents explorer', async ({ page }) => {
  // The org-wide evals page is gone; evals live under each agent's Evals group
  // and under Global Evals. The route is kept as a redirect because /evals has
  // been linked from chat transcripts and bookmarks.
  await page.goto('/evals', { waitUntil: 'commit' });
  await page.waitForURL(/\/agents/, { timeout: 20000 });
  await expect(page).toHaveURL(/\/agents/);
});

test('global evals are reachable from the agents tree', async ({ page }) => {
  await page.goto('/agents', { waitUntil: 'commit' });
  // Org-level eval admins get the Global Evals shelf at the top of the tree.
  await expect(page.getByText('Global Evals').first())
    .toBeVisible({ timeout: 20000 });
});
