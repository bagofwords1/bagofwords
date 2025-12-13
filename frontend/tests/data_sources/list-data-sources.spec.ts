
import { test, expect } from '@playwright/test';

test('can list data sources', async ({ page }) => {
  await page.goto('/integrations');
  await page.waitForLoadState('networkidle');

  // Check that integrations page loads (either connected or available section)
  await expect(
    page.getByText('Connected Integrations').or(page.getByText('Available Integrations'))
  ).toBeVisible({ timeout: 10000 });
});
