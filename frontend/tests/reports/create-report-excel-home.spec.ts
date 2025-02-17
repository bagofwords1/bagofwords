import { test, expect } from '@playwright/test';
import { createReport } from '../utils/helpers';

test('can create a new report from excel home', async ({ page }) => {
  await createReport(page);
  
  // Verify we're on the new report page
  expect(page.url()).toMatch(/\/excel\/reports\/.+/);
});
