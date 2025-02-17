
import { test, expect } from '@playwright/test';

test('can list data sources', async ({ page }) => {
  // Navigate to excel home page
  await page.goto('/integrations');

  // Check for "Available Integrations" heading
  await expect(page.getByText('Available Integrations')).toBeVisible();
  
});
