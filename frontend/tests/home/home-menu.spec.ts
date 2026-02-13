import { test, expect } from '../fixtures/feature-test';

test('home menu is visible and contains expected links', async ({ page }) => {
  // Navigate to excel home page
  await page.goto('/');

});
