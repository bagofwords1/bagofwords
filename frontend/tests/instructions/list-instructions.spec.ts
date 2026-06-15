import { test, expect } from '../fixtures/feature-test';

test('can view instructions page', async ({ page }) => {
  // The instructions page is now the Agents explorer at /agents.
  await page.goto('/agents');
  await page.waitForLoadState('networkidle');

  // Verify page heading (longer timeout for CI).
  // The instructions page is now the "Agents" knowledge explorer.
  await expect(page.getByRole('heading', { name: 'Agents', exact: true }))
    .toBeVisible({ timeout: 15000 });

  // Verify page description
  await expect(page.getByText('The instructions, rules and skills your agents reason with'))
    .toBeVisible({ timeout: 10000 });
});

