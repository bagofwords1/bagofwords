import { test, expect } from '../fixtures/feature-test';

test('can view instructions page', async ({ page }) => {
  // The instructions page is now the Agents explorer at /agents.
  // The explorer fires several mount-time fetches, so the browser 'load' event
  // is unreliable on CI (goto can hang until the per-test timeout). Resolve the
  // navigation as soon as it commits and wait on concrete content instead.
  await page.goto('/agents', { waitUntil: 'commit' });

  // Verify page heading (longer timeout for CI).
  // The instructions page is now the "Agents" knowledge explorer.
  await expect(page.getByRole('heading', { name: 'Agents', exact: true }))
    .toBeVisible({ timeout: 30000 });

  // Verify page description
  await expect(page.getByText('Configure your agents and the data, tools, skills and instructions they reason with'))
    .toBeVisible({ timeout: 10000 });
});

