import { test, expect } from '../fixtures/feature-test';

test('can view settings page', async ({ page }) => {
  await page.goto('/settings', { waitUntil: 'commit' });

  // /settings redirects to /settings/members; the heading names the current
  // page (the admin sidebar replaced the tab strip). Longer timeout for CI.
  await expect(page.getByRole('heading', { name: 'Members', exact: true }))
    .toBeVisible({ timeout: 15000 });

  // Admin nav entries are present in the sidebar. The members entry is
  // labelled "Access" (settings.membersTab).
  await expect(page.getByRole('link', { name: 'Access' }))
    .toBeVisible({ timeout: 10000 });
  await expect(page.getByRole('link', { name: 'LLM' }))
    .toBeVisible({ timeout: 10000 });
});

