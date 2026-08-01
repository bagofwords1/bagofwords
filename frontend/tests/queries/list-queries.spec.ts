import { test, expect } from '../fixtures/feature-test';

test('can view queries page', async ({ page }) => {
  await page.goto('/queries', { waitUntil: 'commit' });

  // On a fresh org the page renders the empty state — the chrome (page
  // <h1>, filter tabs, search input) is hidden until there is data.
  await expect(page.getByRole('heading', { name: 'Nothing published yet' }))
    .toBeVisible({ timeout: 15000 });
});
