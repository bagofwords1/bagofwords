import { test, expect } from '../fixtures/auth';

test.describe('Admin-only page visibility', () => {

  test('admin can access monitoring page', async ({ adminPage }) => {
    await adminPage.goto('/monitoring');
    await adminPage.waitForLoadState('domcontentloaded');

    // Admin should see the monitoring page (longer timeout for CI)
    await expect(adminPage.getByRole('heading', { name: 'Monitoring', exact: true }))
      .toBeVisible({ timeout: 30000 });
    
    // Verify tabs are visible
    await expect(adminPage.getByText('Explore')).toBeVisible({ timeout: 10000 });
  });

  test('member cannot access monitoring page', async ({ memberPage }) => {
    await memberPage.goto('/monitoring');
    await memberPage.waitForLoadState('domcontentloaded');

    // Member should either be redirected away OR see an access denied state
    // Wait for redirect to happen
    try {
      await memberPage.waitForURL((url) => !url.pathname.includes('/monitoring'), { timeout: 20000 });
      // Redirect happened - test passes
      return;
    } catch {
      // No redirect - check that monitoring content isn't accessible
    }
    
    // If still on /monitoring, at least the monitoring heading should NOT be visible
    // (the page should show access denied or empty state)
    const monitoringHeading = memberPage.getByRole('heading', { name: 'Monitoring', exact: true });
    const isMonitoringVisible = await monitoringHeading.isVisible().catch(() => false);
    
    // Either redirected OR monitoring content is not visible
    const url = memberPage.url();
    const wasRedirected = !url.includes('/monitoring');
    
    expect(wasRedirected || !isMonitoringVisible).toBe(true);
  });

  test('admin can see LLM settings tab', async ({ adminPage }) => {
    await adminPage.goto('/settings');
    await adminPage.waitForLoadState('domcontentloaded');

    // Admin should see Settings page
    await expect(adminPage.getByRole('heading', { name: 'Settings', exact: true }))
      .toBeVisible({ timeout: 10000 });

    // Admin should see LLM tab
    await expect(adminPage.getByRole('link', { name: 'LLM' }))
      .toBeVisible();
  });

  // Note: Members CAN see LLM tab but it's read-only (they cannot edit)
  test('member can see LLM tab (read-only)', async ({ memberPage }) => {
    await memberPage.goto('/settings');
    await memberPage.waitForLoadState('domcontentloaded');

    // Member CAN see LLM tab (but it's read-only)
    const llmTab = memberPage.getByRole('link', { name: 'LLM' });
    await expect(llmTab).toBeVisible({ timeout: 5000 });

    // Navigate to LLM settings
    await llmTab.click();
    await memberPage.waitForLoadState('domcontentloaded');

    // Member should NOT see "Add Provider" button (admin-only action)
    const addProviderButton = memberPage.getByRole('button', { name: 'Add Provider' });
    await expect(addProviderButton).not.toBeVisible({ timeout: 3000 });

    // Member should NOT see "Actions" column header (Edit/Delete)
    const actionsHeader = memberPage.locator('th').filter({ hasText: 'Actions' });
    await expect(actionsHeader).not.toBeVisible({ timeout: 3000 });
  });

  test('admin can see Add Member button', async ({ adminPage }) => {
    await adminPage.goto('/settings/members');
    await adminPage.waitForLoadState('domcontentloaded');

    // Admin should see Add Member button
    await expect(adminPage.getByRole('button', { name: 'Add Member' }))
      .toBeVisible({ timeout: 10000 });
  });

  test('member cannot see Add Member button', async ({ memberPage }) => {
    await memberPage.goto('/settings/members');
    await memberPage.waitForLoadState('domcontentloaded');

    // Member should NOT see Add Member button
    const addButton = memberPage.getByRole('button', { name: 'Add Member' });
    await expect(addButton).not.toBeVisible({ timeout: 5000 });
  });

  test('admin can access evals page', async ({ adminPage }) => {
    await adminPage.goto('/evals');
    await adminPage.waitForLoadState('domcontentloaded');

    // Admin should see the evals page content
    // Check for the "Total Test Cases" metric card (unique element)
    await expect(adminPage.getByText('Total Test Cases', { exact: true })).toBeVisible({ timeout: 10000 });
  });

  test('member cannot access evals page', async ({ memberPage }) => {
    await memberPage.goto('/evals');
    await memberPage.waitForLoadState('domcontentloaded');

    // Member should be redirected away from /evals (middleware redirect)
    // Wait for redirect to actually happen
    try {
      await memberPage.waitForURL((url) => !url.pathname.includes('/evals'), { timeout: 15000 });
    } catch {
      // If no redirect happened, the test will fail on the assertion below
    }
    
    const url = memberPage.url();
    
    // Should NOT be on /evals
    expect(url).not.toContain('/evals');
  });
});
