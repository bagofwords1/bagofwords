
import { test, expect } from '../fixtures/feature-test';

test('can list data sources', async ({ page }) => {
  // The legacy data-sources list moved to /old_agents (/agents is now the explorer).
  await page.goto('/old_agents');
  await page.waitForLoadState('networkidle');

  // Wait for page to fully load. The page can render both a "Data Agents" and a
  // "Connections" heading, so scope to the first match to avoid a strict-mode
  // violation when data sources exist.
  await expect(
    page.getByRole('heading', { name: /Data Agents|Connections/ }).first()
  ).toBeVisible({ timeout: 15000 });
});
