import { test, expect } from '@playwright/test';

test('home menu is visible and contains expected links', async ({ page }) => {
  // Navigate to excel home page
  await page.goto('/');

});
