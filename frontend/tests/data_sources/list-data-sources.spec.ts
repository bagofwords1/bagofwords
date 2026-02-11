
import { test, expect } from '../fixtures/feature-test';

test('can list data sources', async ({ page }) => {
  await page.goto('/data');
  await page.waitForLoadState('networkidle');

  // Wait for page to fully load (either Data Agents or Connections section)
  // The page shows "Data Agents" when there are data sources, or "Connections" section always
  await expect(
    page.getByRole('heading', { name: /Data Agents|Connections/ })
  ).toBeVisible({ timeout: 15000 });
});
