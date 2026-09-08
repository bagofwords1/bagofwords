import { test, expect } from '../fixtures/feature-test';

// The agent's Queries panel lets an entity manager author a query by hand —
// no chat step required. The form previews through the stateless
// POST /api/entities/preview and saves through POST /api/entities, attached to
// the agent it was opened from; the new row is then listed and opened.
test('a query can be created by hand from the agent queries panel', async ({ page }) => {
  const AGENT_ID = 'ds-manual-1';
  const agent = {
    id: AGENT_ID, name: 'Manual Agent', type: 'sqlite',
    is_public: true, status: 'active', publish_status: 'published',
    connections: [{ id: 'c1', name: 'main', type: 'sqlite', is_active: true }],
  };
  const listed: any[] = [];
  const created = {
    id: 'ent-manual-1', type: 'model', title: 'Hand-written revenue', slug: 'hand-written-revenue',
    description: null, code: '', data: {}, status: 'published', organization_id: 'org-1',
    owner_id: 'user-1', data_sources: [{ id: AGENT_ID, name: 'Manual Agent', type: 'sqlite' }],
    updated_at: new Date().toISOString(), created_at: new Date().toISOString(),
    pinned: false, auto_refresh_enabled: false, private_status: null, global_status: 'approved',
    reviewed_by_user_id: null, tags: [], parameters: [],
  };
  const previewBodies: any[] = [];
  const createBodies: any[] = [];

  await page.route('**/api/entities/counts**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ by_agent: { [AGENT_ID]: listed.length }, total: listed.length }) }));
  await page.route('**/api/entities?**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(listed) }));
  await page.route('**/api/entities/preview', (route) => {
    previewBodies.push(route.request().postDataJSON());
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ data: { columns: [{ field: 'example' }], rows: [{ example: 1 }], info: { total_rows: 1 } }, execution_log: '' }) });
  });
  await page.route('**/api/entities', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    createBodies.push(route.request().postDataJSON());
    listed.push(created);
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(created) });
  });
  await page.route(`**/api/entities/${created.id}/run`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ...created, data: { columns: [{ field: 'example' }], rows: [{ example: 1 }], info: { total_rows: 1 } } }) }));
  await page.route(`**/api/entities/${created.id}`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ...created, data: { columns: [{ field: 'example' }], rows: [{ example: 1 }], info: { total_rows: 1 } } }) }));
  await page.route('**/api/data_sources/active**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([agent]) }));
  await page.route(`**/api/data_sources/${AGENT_ID}`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(agent) }));

  await page.goto('/agents', { waitUntil: 'commit' });
  await page.getByText('Manual Agent', { exact: true }).first().click();
  await page.getByText('Queries', { exact: true }).first().click();

  // Empty list, but the admin sees the create affordance.
  await expect(page.getByTestId('agent-query-row')).toHaveCount(0);
  await page.getByTestId('agent-query-new').click();
  await expect(page.getByTestId('entity-edit-title')).toHaveText('New query');
  await page.screenshot({ path: (process.env.NEW_QUERY_SHOT || 'test-results/new-query.png').replace('.png', '-top.png') });

  // Same form as the report's Save Query: title, description, agents (this
  // agent preselected), status — plus the code, seeded with the agent's real
  // client key. Save waits for a title.
  const create = page.getByTestId('entity-edit-save');
  await expect(page.getByTestId('entity-form-agents')).toContainText('Manual Agent');
  await expect(create).toBeDisabled();
  await page.getByPlaceholder('Revenue by month').fill('Hand-written revenue');
  await page.getByPlaceholder('Description').fill('Revenue, by hand');
  await expect(create).toBeEnabled();

  // Code tab: the starter code addresses this agent's real client key.
  await page.getByTestId('entity-edit-tab-code').click();
  await expect(page.getByText('ds_clients["Manual Agent:main"]').first()).toBeVisible({ timeout: 15000 });

  // Parameters (same tab): declaring one adds `params` to the signature, and
  // its test value travels with the preview, like a dashboard query's controls.
  await page.getByTestId('entity-edit-add-param').click();
  await page.getByTestId('entity-param-name-0').fill('country');
  await page.getByTestId('entity-param-test-0').fill('FR');
  await expect(page.getByText('def generate_df(ds_clients, excel_files, params):').first()).toBeVisible();
  // Naming the parameter scaffolds it into the SQL and the execute_query call.
  await expect(page.getByText('WHERE (:country IS NULL OR country = :country)').first()).toBeVisible();

  // A fixed choice list: the mode sticks before anything is typed, and the
  // values travel with the declaration.
  await page.getByTestId('entity-param-more-0').click();
  await page.getByTestId('entity-param-choices-0').click();
  await page.getByRole('option', { name: 'Fixed list' }).click();
  await page.getByPlaceholder('IL, FR, DE').fill('IL, FR');
  await page.getByTestId('entity-param-more-0').click();

  // Run previews against the agent without creating anything.
  await page.getByTestId('entity-edit-run').click();
  await expect.poll(() => previewBodies.length).toBe(1);
  expect(previewBodies[0].data_source_ids).toEqual([AGENT_ID]);
  expect(previewBodies[0].code).toContain('ds_clients["Manual Agent:main"]');
  expect(previewBodies[0].parameters).toEqual([{ name: 'country', type: 'string', source: 'input', required: false, options: ['IL', 'FR'] }]);
  expect(previewBodies[0].code).toContain('params={"country": params.get("country")}');
  expect(previewBodies[0].params).toEqual({ country: 'FR' });
  expect(createBodies).toHaveLength(0);
  await expect(page.getByTestId('entity-edit-result')).toContainText('1 rows');
  // The result scrolls itself into view; give the smooth scroll a moment.
  await page.waitForTimeout(700);
  await expect(page.getByTestId('entity-edit-result')).toBeInViewport({ ratio: 0.95 });
  await page.screenshot({ path: process.env.NEW_QUERY_SHOT || 'test-results/new-query.png' });
  await page.getByTestId('entity-edit-params-toggle').click();
  await page.getByTestId('entity-param-more-0').click();
  await page.screenshot({ path: (process.env.NEW_QUERY_SHOT || 'test-results/new-query.png').replace('.png', '-params.png') });

  // Removing a parameter takes its scaffold back out of the code (the block
  // was reopened above for the screenshot).
  await page.getByTestId('entity-edit-add-param').click();
  await page.getByTestId('entity-param-name-1').fill('city');
  await page.getByTestId('entity-param-name-1').press('Tab');
  await expect(page.getByText('AND (:city IS NULL OR city = :city)').first()).toBeVisible();
  await page.getByTestId('entity-param-row-1').getByTitle('Remove').click();
  await expect(page.getByText('AND (:city IS NULL OR city = :city)')).toHaveCount(0);
  await expect(page.getByText('WHERE (:country IS NULL OR country = :country)').first()).toBeVisible();

  // Create mints the row on this agent, then the panel opens it.
  await create.click();
  await expect.poll(() => createBodies.length).toBe(1);
  expect(createBodies[0]).toMatchObject({
    title: 'Hand-written revenue', description: 'Revenue, by hand', type: 'model', status: 'published',
    data_source_ids: [AGENT_ID],
  });
  expect(createBodies[0].code).toContain('def generate_df(ds_clients, excel_files, params)');
  expect(createBodies[0].parameters).toEqual([{ name: 'country', type: 'string', source: 'input', required: false, options: ['IL', 'FR'] }]);
  await expect(page).toHaveURL(new RegExp(`/agents/queries/${created.id}`), { timeout: 15000 });
  await expect(page.getByText('Hand-written revenue').first()).toBeVisible();

  // Edit opens the same form, prefilled from the saved query.
  await page.getByRole('button', { name: 'Edit', exact: true }).click();
  await expect(page.getByTestId('entity-edit-title')).toHaveText('Edit query');
  await expect(page.getByPlaceholder('Revenue by month')).toHaveValue('Hand-written revenue');
  await expect(page.getByTestId('entity-form-agents')).toContainText('Manual Agent');
  await page.screenshot({ path: (process.env.NEW_QUERY_SHOT || 'test-results/new-query.png').replace('.png', '-edit.png') });
});
