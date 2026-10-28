import { test, expect } from '../fixtures/feature-test';

// Clone agent (Knowledge Explorer → agent menu → "Clone agent"): the dialog
// creates the new agent and opens it. Sharing a list with the clone and then
// taking it off the source also exercises the old-link fallback: the source's
// list URL opens the list on the agent it lives on now. What each choice
// copies, shares or leaves out is covered by the backend e2e suite
// (tests/e2e/rbac/test_agent_clone*.py).
test('clone an agent from its menu, and an old list link follows the list', async ({ page }) => {
  const cookies = await page.context().cookies();
  const rawToken = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token')?.value;
  expect(rawToken, 'auth token cookie should exist in the admin storage state').toBeTruthy();
  const bearer = decodeURIComponent(rawToken!);
  const auth = bearer.startsWith('Bearer') ? bearer : `Bearer ${bearer}`;
  const whoami = await page.request.get('/api/users/whoami', { headers: { Authorization: auth } });
  const orgId = (await whoami.json()).organizations?.[0]?.id;
  const headers = { Authorization: auth, 'X-Organization-Id': orgId };

  const sourceName = `Clone Source ${Date.now()}`;
  const created = await page.request.post('/api/data_sources', {
    headers,
    data: { name: sourceName, type: 'network_dir', config: { root_path: '/tmp' }, credentials: { auth_type: 'none' }, auth_policy: 'system_only' },
  });
  expect(created.ok(), `create agent failed: ${created.status()}`).toBeTruthy();
  const sourceId = (await created.json()).id;
  const list = await page.request.post(`/api/data_sources/${sourceId}/lists`, {
    headers,
    data: { name: 'Complaints', fields: [{ name: 'customer', type: 'string', required: true }], key_field: 'customer' },
  });
  expect(list.ok(), `create list failed: ${list.status()}`).toBeTruthy();
  const listId = (await list.json()).id;

  // Clone through the dialog, sharing the list.
  await page.goto(`/agents/${sourceId}`, { waitUntil: 'commit' });
  await expect(page.getByTestId('agent-hero')).toBeVisible({ timeout: 45000 });
  await page.locator('button[aria-label="Agent actions"]').click();
  await page.getByRole('menuitem', { name: 'Clone agent' }).click();
  await expect(page.getByTestId('agent-clone-modal')).toBeVisible();
  const cloneName = `${sourceName} copy`;
  await page.getByTestId('agent-clone-name').fill(cloneName);
  await page.getByTestId('agent-clone-lists-link').click();
  await expect(page.getByTestId('agent-clone-hint-lists')).toHaveText('Same list and rows on both');
  await page.getByTestId('agent-clone-submit').click();

  // The new agent opens.
  await expect(page.getByTestId('agent-clone-modal')).toBeHidden({ timeout: 30000 });
  await expect(page.getByTestId('agent-hero')).toContainText(cloneName, { timeout: 30000 });
  const agents = await (await page.request.get('/api/data_sources', { headers })).json();
  const clone = agents.find((a: any) => a.name === cloneName);
  expect(clone, 'the clone should exist').toBeTruthy();
  await expect(page).toHaveURL(new RegExp(`/agents/${clone.id}`));

  // Take the list off the source; its old address opens it on the clone.
  const removed = await page.request.delete(`/api/data_sources/${sourceId}/lists/${listId}?scope=agent`, { headers });
  expect(removed.status()).toBe(204);
  await page.goto(`/agents/${sourceId}/lists/${listId}`, { waitUntil: 'commit' });
  await expect(page.getByTestId('list-title')).toHaveText('Complaints', { timeout: 45000 });
  await expect(page).toHaveURL(new RegExp(`/agents/${clone.id}/lists/${listId}`));
});
