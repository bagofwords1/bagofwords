import { test, expect } from '../fixtures/feature-test';

// The sidebar "Group and sort" menu used a full-screen transparent backdrop to
// detect outside clicks. That backdrop ate the next click: with the menu open,
// pressing a report row's ⋯ (right below the trigger) only closed the menu and
// the row menu needed a second click. Outside presses must now close the menu
// AND reach their target in the same click.
test('an outside click closes the group/sort menu and still reaches its target', async ({ page }) => {
  const cookies = await page.context().cookies();
  const rawToken = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token')?.value;
  expect(rawToken, 'auth token cookie should exist in the admin storage state').toBeTruthy();
  const bearer = decodeURIComponent(rawToken!);
  const auth = bearer.startsWith('Bearer') ? bearer : `Bearer ${bearer}`;
  const whoami = await page.request.get('/api/users/whoami', { headers: { Authorization: auth } });
  expect(whoami.ok()).toBeTruthy();
  const orgId = (await whoami.json()).organizations?.[0]?.id;
  const headers = { Authorization: auth, 'X-Organization-Id': orgId };

  // The trigger only renders once the sidebar has reports.
  const title = `View menu ${Date.now().toString(36)}`;
  const created = await page.request.post('/api/reports', { headers, data: { title } });
  expect(created.ok(), `create report failed: ${created.status()}`).toBeTruthy();
  const report = await created.json();

  try {
    await page.goto('/reports', { waitUntil: 'commit' });
    const trigger = page.getByTestId('reports-view-menu-trigger');
    const groupByRow = page.getByRole('button', { name: /Group by/ });
    await expect(trigger).toBeVisible({ timeout: 20000 });

    // Trigger still toggles; a press inside the panel keeps it open.
    await trigger.click();
    await expect(groupByRow).toBeVisible();
    await groupByRow.click();
    await expect(groupByRow).toBeVisible();
    await trigger.click();
    await expect(groupByRow).toBeHidden();

    // One click on the row's ⋯ closes the menu and opens the row menu.
    await trigger.click();
    await expect(groupByRow).toBeVisible();
    const row = page.locator('li.group\\/report', { hasText: title });
    await row.hover();
    await row.getByRole('button', { name: 'Report actions' }).click();
    await expect(groupByRow).toBeHidden();
    await expect(page.getByRole('button', { name: 'Rename' })).toBeVisible();
  } finally {
    await page.request.delete(`/api/reports/${report.id}`, { headers });
  }
});
