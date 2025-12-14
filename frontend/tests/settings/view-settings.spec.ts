import { test, expect } from '@playwright/test';

test('can view settings page', async ({ page }) => {
  await page.goto('/settings');
  await page.waitForLoadState('domcontentloaded');

  // Verify page heading
  await expect(page.getByRole('heading', { name: 'Settings', exact: true }))
    .toBeVisible({ timeout: 10000 });

  // Verify settings tabs are present (redirects to /settings/members)
  await expect(page.getByRole('link', { name: 'Members' }))
    .toBeVisible();
  await expect(page.getByRole('link', { name: 'LLM' }))
    .toBeVisible();
});

