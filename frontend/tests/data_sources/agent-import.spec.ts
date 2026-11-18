import { test, expect } from '../fixtures/feature-test';

// Import agent (Agents › New › Import agent): a bundle downloaded with
// "Export agent" becomes a NEW agent through the wizard — upload, map the
// bundle's connection, review, import, then land on the agent. The backend
// contract (re-mapping onto other connections, missing tables/members, never
// overwriting) is covered by backend/tests/e2e/rbac/test_agent_bundle_import.py;
// this spec covers the user-facing flow end to end.
test('agent manager imports an exported agent from a zip', async ({ page }) => {
  const cookies = await page.context().cookies();
  const rawToken = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token')?.value;
  expect(rawToken, 'auth token cookie should exist in the admin storage state').toBeTruthy();
  const bearer = decodeURIComponent(rawToken!);
  const auth = bearer.startsWith('Bearer') ? bearer : `Bearer ${bearer}`;
  const whoami = await page.request.get('/api/users/whoami', { headers: { Authorization: auth } });
  const orgId = (await whoami.json()).organizations?.[0]?.id;
  const headers = { Authorization: auth, 'X-Organization-Id': orgId };

  // A source agent with one instruction, exported through the real route.
  const stamp = Date.now();
  const sourceName = `Import Source ${stamp}`;
  const created = await page.request.post('/api/data_sources', {
    headers,
    data: { name: sourceName, type: 'network_dir', config: { root_path: '/tmp' }, credentials: { auth_type: 'none' }, auth_policy: 'system_only' },
  });
  expect(created.ok(), `create agent failed: ${created.status()}`).toBeTruthy();
  const sourceId = (await created.json()).id;
  const instr = await page.request.post('/api/instructions', {
    headers,
    data: { text: `Imported rule ${stamp}`, status: 'published', category: 'general', data_source_ids: [sourceId] },
  });
  expect(instr.ok()).toBeTruthy();
  const exported = await page.request.get(`/api/data_sources/${sourceId}/instructions/export`, { headers });
  expect(exported.ok(), `export failed: ${exported.status()}`).toBeTruthy();
  const zip = Buffer.from(await exported.body());

  // Agents › New › Import agent.
  await page.goto('/agents', { waitUntil: 'commit' });
  await page.locator('button.bg-blue-600.pe-2').first().click({ timeout: 45000 });
  await page.getByTestId('menu-import-agent').click();
  await expect(page.getByTestId('import-agent-wizard')).toBeVisible();

  // Upload → the bundle summary and its connection, auto-matched by name.
  await page.getByTestId('import-file-input').setInputFiles({ name: 'agent-export.zip', mimeType: 'application/zip', buffer: zip });
  await expect(page.getByTestId('bundle-summary')).toContainText(sourceName, { timeout: 20000 });
  await expect(page.locator('[data-testid^="map-row-"]')).toHaveCount(1);
  await page.getByTestId('import-continue').click();

  // Review: the source name is taken here, so a free name is suggested.
  const nameInput = page.locator('input[data-testid="import-name"], [data-testid="import-name"] input').first();
  await expect(nameInput).toHaveValue(`${sourceName} (imported)`);
  const importedName = `Imported ${stamp}`;
  await nameInput.fill(importedName);
  await page.getByTestId('import-submit').click();

  await expect(page.getByTestId('import-result')).toContainText(importedName, { timeout: 60000 });
  await page.getByTestId('import-open-agent').click();
  await expect(page).toHaveURL(/\/agents\/[0-9a-f-]{36}/);

  // The new agent exists next to the source, with the instruction as a draft.
  const agents = await (await page.request.get('/api/data_sources', { headers })).json();
  const imported = agents.find((a: any) => a.name === importedName);
  expect(imported, 'imported agent should exist').toBeTruthy();
  expect(agents.some((a: any) => a.id === sourceId)).toBeTruthy();
  const list = await (await page.request.get('/api/instructions', {
    headers, params: { data_source_ids: imported.id, include_global: 'false', include_drafts: 'true' },
  })).json();
  const items = Array.isArray(list) ? list : list.items;
  expect(items.map((i: any) => [i.text, i.status])).toEqual([[`Imported rule ${stamp}`, 'draft']]);
});
