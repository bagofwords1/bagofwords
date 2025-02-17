import { test, expect } from '@playwright/test';

test('home menu is visible and contains expected links', async ({ page }) => {
  // Navigate to excel home page
  await page.goto('/excel');

  // Click hamburger menu button
  await page.click('button[aria-haspopup="menu"]');
  
  // Verify dropdown menu appears
  await expect(page.locator('div[aria-haspopup="menu"]')).toBeVisible();

  // Verify menu items are present and visible
  await expect(page.getByRole('menuitem', { name: 'Reports' })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: 'Memory' })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: 'Integrations' })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: 'Logout' })).toBeVisible();
});
