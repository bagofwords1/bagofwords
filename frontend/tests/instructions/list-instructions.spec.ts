import { test, expect } from '../fixtures/feature-test';

test('can view instructions page', async ({ page }) => {
  await page.goto('/instructions');
  await page.waitForLoadState('networkidle');

  // Verify page heading (longer timeout for CI).
  // The instructions page is now the "Agents" knowledge explorer.
  await expect(page.getByRole('heading', { name: 'Agents', exact: true }))
    .toBeVisible({ timeout: 15000 });

  // Verify page description
  await expect(page.getByText('The instructions, rules and skills your agents reason with'))
    .toBeVisible({ timeout: 10000 });
});

