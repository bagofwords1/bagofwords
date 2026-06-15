import { test, expect } from '../fixtures/feature-test';

test('can view settings page', async ({ page }) => {
  await page.goto('/settings');
  await page.waitForLoadState('networkidle');

  // Verify page heading (longer timeout for CI)
  await expect(page.getByRole('heading', { name: 'Settings', exact: true }))
    .toBeVisible({ timeout: 15000 });

  // Verify settings tabs are present (redirects to /settings/members).
  // The members tab is now labelled "Access" (settings.membersTab).
  await expect(page.getByRole('link', { name: 'Access' }))
    .toBeVisible({ timeout: 10000 });
  await expect(page.getByRole('link', { name: 'LLM' }))
    .toBeVisible({ timeout: 10000 });
});

