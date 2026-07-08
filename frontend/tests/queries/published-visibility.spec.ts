import { test, expect } from '../fixtures/feature-test';

// Regression: a published (global/approved) entity must render on the Queries
// page. Previously the full-page "Nothing published yet" empty state was gated
// on a `items` ref that was never populated from the fetch, so any org with
// published entities but no drafts/suggestions saw the empty state and its
// published list stayed hidden.
test('published entities render instead of the empty state', async ({ page }) => {
  const published = {
    id: 'reg-published-1',
    type: 'model',
    title: 'Regression Published Entity',
    slug: 'regression-published-entity',
    description: 'Should be visible under the Published tab',
    status: 'published',
    organization_id: 'org-1',
    owner_id: 'user-1',
    data_sources: [],
    updated_at: new Date().toISOString(),
    pinned: false,
    auto_refresh_enabled: false,
    private_status: null,
    global_status: 'approved',
    reviewed_by_user_id: null,
  };

  // Intercept the entities list the page fetches on mount.
  await page.route('**/api/entities**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([published]),
    });
  });

  await page.goto('/queries');
  await page.waitForLoadState('networkidle');

  // The published entity is listed...
  await expect(page.getByText('Regression Published Entity')).toBeVisible({ timeout: 15000 });
  // ...and the full-page empty state is NOT shown.
  await expect(page.getByRole('heading', { name: 'Nothing published yet' })).toHaveCount(0);
});
