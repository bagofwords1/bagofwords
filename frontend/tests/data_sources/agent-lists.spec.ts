import { test, expect } from '../fixtures/feature-test';

// Agent Lists in the Knowledge Explorer (docs/feedback-loops/agent-lists.md):
// an agent manager can create a list with a schema from the Lists row, land on
// its (empty) rows view, edit its fields, and delete it — and the tree badge
// follows. Rows are written by the agent's submit_<list> tool, which the
// backend e2e suite covers (tests/e2e/rbac/test_agent_lists.py).
test('agent manager creates, edits and deletes a list', async ({ page }) => {
  const cookies = await page.context().cookies();
  const rawToken = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token')?.value;
  expect(rawToken, 'auth token cookie should exist in the admin storage state').toBeTruthy();
  const bearer = decodeURIComponent(rawToken!);
  const auth = bearer.startsWith('Bearer') ? bearer : `Bearer ${bearer}`;
  const whoami = await page.request.get('/api/users/whoami', { headers: { Authorization: auth } });
  const orgId = (await whoami.json()).organizations?.[0]?.id;
  const headers = { Authorization: auth, 'X-Organization-Id': orgId };

  const agentName = `Lists Agent ${Date.now()}`
  const created = await page.request.post('/api/data_sources', {
    headers,
    data: { name: agentName, type: 'network_dir', config: { root_path: '/tmp' }, credentials: { auth_type: 'none' }, auth_policy: 'system_only' },
  });
  expect(created.ok(), `create agent failed: ${created.status()}`).toBeTruthy();
  const agentId = (await created.json()).id;

  await page.goto(`/agents/${agentId}/lists`, { waitUntil: 'commit' });
  await expect(page.getByTestId('agent-lists-empty')).toBeVisible({ timeout: 45000 });

  // Create a list with a key field and a choice field.
  await page.getByTestId('agent-list-new').click();
  await page.getByTestId('list-name').fill('Invoices');
  const first = page.getByTestId('list-field').first();
  await first.getByTestId('list-field-name').fill('Invoice Number');
  await expect(first.getByTestId('list-field-name')).toHaveValue('invoice_number');
  await first.getByTestId('list-field-required').check();
  await first.getByTestId('list-field-key').click();
  await page.getByTestId('list-add-field').click();
  const second = page.getByTestId('list-field').nth(1);
  await second.getByTestId('list-field-name').fill('status');
  await second.getByTestId('list-field-type').selectOption('enum');
  await second.getByTestId('list-field-enum').fill('open, paid');
  await page.getByTestId('list-editor-save').click();

  await expect(page.getByTestId('list-title')).toHaveText('Invoices', { timeout: 15000 });
  await expect(page.getByTestId('list-rows-empty')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/agents/${agentId}/lists/[0-9a-f-]{36}$`));

  const api = await page.request.get(`/api/data_sources/${agentId}/lists`, { headers });
  const lists = await api.json();
  expect(lists).toHaveLength(1);
  expect(lists[0].key_field).toBe('invoice_number');
  expect(lists[0].fields.map((f: any) => f.name)).toEqual(['invoice_number', 'status']);

  // Edit: add an optional field (additive — version stays 1).
  await page.getByTestId('list-more').click();
  await page.getByRole('menuitem', { name: 'Edit fields' }).click();
  await page.getByTestId('list-add-field').click();
  await page.getByTestId('list-field').nth(2).getByTestId('list-field-name').fill('amount');
  await page.getByTestId('list-field').nth(2).getByTestId('list-field-type').selectOption('number');
  await page.getByTestId('list-editor-save').click();
  await expect(page.getByTestId('list-rows-view')).toBeVisible({ timeout: 15000 });
  const after = await (await page.request.get(`/api/data_sources/${agentId}/lists`, { headers })).json();
  expect(after[0].fields).toHaveLength(3);
  expect(after[0].version).toBe(1);

  // Delete through the confirm dialog; the index is empty again.
  await page.getByTestId('list-more').click();
  await page.getByRole('menuitem', { name: 'Delete list' }).click();
  await page.getByTestId('list-delete-confirm-button').click();
  await expect(page.getByTestId('agent-lists-empty')).toBeVisible({ timeout: 15000 });
  const gone = await (await page.request.get(`/api/data_sources/${agentId}/lists`, { headers })).json();
  expect(gone).toEqual([]);
});
