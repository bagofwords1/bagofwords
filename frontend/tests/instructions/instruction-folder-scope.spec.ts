import { test, expect } from '../fixtures/feature-test';

// A folder-scoped instruction has no agents of its own (`data_sources` is
// empty), exactly like an org-wide one. The instruction modal used to read
// only `data_sources`, so it labelled a folder rule "All agents" and the
// instructions table showed "—". Both must name the folder, while a truly
// global instruction keeps "All agents".
test('a folder-scoped instruction shows its folder, not "All agents"', async ({ page }) => {
  const cookies = await page.context().cookies();
  const rawToken = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token')?.value;
  expect(rawToken, 'auth token cookie should exist in the admin storage state').toBeTruthy();
  const bearer = decodeURIComponent(rawToken!);
  const auth = bearer.startsWith('Bearer') ? bearer : `Bearer ${bearer}`;
  const whoami = await page.request.get('/api/users/whoami', { headers: { Authorization: auth } });
  expect(whoami.ok()).toBeTruthy();
  const orgId = (await whoami.json()).organizations?.[0]?.id;
  expect(orgId, 'admin user should belong to an organization').toBeTruthy();
  const headers = { Authorization: auth, 'X-Organization-Id': orgId };

  const tag = Date.now().toString(36);
  const folderName = `Scope folder ${tag}`;
  const folderRes = await page.request.post('/api/folders', { headers, data: { name: folderName, parent_id: null } });
  expect(folderRes.ok(), `create folder failed: ${folderRes.status()}`).toBeTruthy();
  const folder = await folderRes.json();

  const create = async (data: Record<string, unknown>) => {
    const res = await page.request.post('/api/instructions', {
      headers,
      data: { status: 'published', category: 'general', load_mode: 'always', data_source_ids: [], ...data },
    });
    expect(res.ok(), `create instruction failed: ${res.status()}`).toBeTruthy();
    return res.json();
  };
  const folderText = `Folder rule ${tag}: include the whole last day of a date range.`;
  const globalText = `Global rule ${tag}: format money with two decimals.`;
  const folderInst = await create({
    title: `Folder rule ${tag}`, text: folderText, owner_folder_id: folder.id, folder_ids: [folder.id],
  });
  const globalInst = await create({ title: `Global rule ${tag}`, text: globalText });

  try {
    await page.goto('/settings/instructions', { waitUntil: 'commit' });
    const search = page.getByPlaceholder('Search instructions...');
    await expect(search).toBeVisible({ timeout: 45000 });
    await search.fill(tag);

    // Table: the folder row names its folder instead of "—".
    const folderRow = page.locator('tr', { hasText: folderText });
    await expect(folderRow).toBeVisible({ timeout: 30000 });
    await expect(folderRow.getByTestId('instruction-row-folder')).toContainText(folderName);

    // Modal: the "Agents" row shows the folder chip, and never "All agents".
    await folderRow.getByText(folderText).click();
    const scopeRow = page.getByTestId('instruction-scope-agents');
    await expect(scopeRow.getByTestId('instruction-scope-folder')).toContainText(folderName, { timeout: 30000 });
    await expect(scopeRow).not.toContainText('All agents');
    await page.keyboard.press('Escape');
    await expect(scopeRow).toBeHidden();

    // A real org-wide instruction is unchanged: still "All agents", no folder.
    await page.locator('tr', { hasText: globalText }).getByText(globalText).click();
    await expect(scopeRow).toContainText('All agents', { timeout: 30000 });
    await expect(scopeRow.getByTestId('instruction-scope-folder')).toHaveCount(0);
  } finally {
    for (const inst of [folderInst, globalInst]) {
      await page.request.delete(`/api/instructions/${inst.id}`, { headers });
    }
    await page.request.delete(`/api/folders/${folder.id}`, { headers });
  }
});
