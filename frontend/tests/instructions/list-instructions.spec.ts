import { test, expect } from '../fixtures/feature-test';

test('can view instructions page', async ({ page }) => {
  // The instructions page is now the Agents explorer at /agents.
  await page.goto('/agents');

  // The explorer fires several data fetches on mount, so 'networkidle' is
  // unreliable on CI — wait on concrete content instead.
  // Verify page heading (longer timeout for CI).
  // The instructions page is now the "Agents" knowledge explorer.
  await expect(page.getByRole('heading', { name: 'Agents', exact: true }))
    .toBeVisible({ timeout: 30000 });

  // Verify page description
  await expect(page.getByText('Configure your agents and the data, tools, skills and instructions they reason with'))
    .toBeVisible({ timeout: 10000 });
});

