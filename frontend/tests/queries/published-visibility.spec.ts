import { test, expect } from '../fixtures/feature-test';

// Regression: a published (global/approved) query must render as a leaf under
// every agent it reads. The catalog moved from the standalone /queries page
// into the /agents tree, so the failure this guards against moved with it —
// previously the page's empty state hid a populated list; now the risk is the
// Queries group drawing its badge but never listing the rows behind it.
test('a published query renders under its agent in the tree', async ({ page }) => {
  const AGENT_ID = 'ds-visible-1';
  const published = {
    id: 'reg-published-1',
    type: 'model',
    title: 'Regression Published Entity',
    slug: 'regression-published-entity',
    description: 'Should be listed under its agent',
    status: 'published',
    organization_id: 'org-1',
    owner_id: 'user-1',
    data_sources: [{ id: AGENT_ID, name: 'Visible Agent', type: 'sqlite' }],
    updated_at: new Date().toISOString(),
    pinned: false,
    auto_refresh_enabled: false,
    private_status: null,
    global_status: 'approved',
    reviewed_by_user_id: null,
  };

  // The per-agent badge and the rows come from two different endpoints; mock
  // both so the test proves they agree rather than that one of them works.
  await page.route('**/api/entities/counts**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ by_agent: { [AGENT_ID]: 1 }, total: 1 }),
    }));
  await page.route('**/api/entities?**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([published]),
    }));
  await page.route('**/api/data_sources/active**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{
        id: AGENT_ID, name: 'Visible Agent', type: 'sqlite',
        is_public: true, status: 'active', publish_status: 'published',
      }]),
    }));

  await page.goto('/agents', { waitUntil: 'commit' });

  await page.getByText('Visible Agent', { exact: true }).first().click();
  await page.getByText('Queries', { exact: true }).first().click();

  await expect(page.getByText('Regression Published Entity', { exact: true }))
    .toBeVisible({ timeout: 15000 });
});
