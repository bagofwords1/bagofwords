import { test, expect } from '../fixtures/feature-test';

// The Evals panel's "Run evals" modal must run what the tree's "Run this
// suite" runs: active cases only (run_suite → _get_cases(status='active')).
// It used to send every listed case — drafts and archived included — and to
// build its list from the Tests-tab search results, so a leftover search term
// silently narrowed an "All suites" run.
test('Run evals modal runs only active cases and ignores the Tests search', async ({ page }) => {
  const cookies = await page.context().cookies();
  const rawToken = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token')?.value;
  expect(rawToken, 'auth token cookie should exist in the admin storage state').toBeTruthy();
  const bearer = decodeURIComponent(rawToken!);
  const auth = bearer.startsWith('Bearer') ? bearer : `Bearer ${bearer}`;
  const whoami = await page.request.get('/api/users/whoami', { headers: { Authorization: auth } });
  const orgId = (await whoami.json()).organizations?.[0]?.id;
  const headers = { Authorization: auth, 'X-Organization-Id': orgId };

  const created = await page.request.post('/api/data_sources', {
    headers,
    data: { name: `Eval Modal Agent ${Date.now()}`, type: 'network_dir', config: { root_path: '/tmp' }, credentials: { auth_type: 'none' }, auth_policy: 'system_only' },
  });
  expect(created.ok(), `create agent failed: ${created.status()}`).toBeTruthy();
  const agentId = (await created.json()).id;

  const createSuite = async (name: string) => {
    const r = await page.request.post('/api/tests/suites', { headers, data: { name, data_source_id: agentId } });
    expect(r.ok(), `create suite failed: ${r.status()}`).toBeTruthy();
    return (await r.json()).id as string;
  };
  const createCase = async (suiteId: string, prompt: string, status: 'active' | 'draft' | 'archived') => {
    const r = await page.request.post(`/api/tests/suites/${suiteId}/cases`, {
      headers,
      data: {
        name: prompt,
        prompt_json: { content: prompt },
        expectations_json: { spec_version: 1, rules: [], order_mode: 'flexible' },
        data_source_ids_json: [agentId],
      },
    });
    expect(r.ok(), `create case failed: ${r.status()}`).toBeTruthy();
    const id = (await r.json()).id as string;
    if (status !== 'active') {
      const s = await page.request.patch(`/api/tests/cases/${id}/status`, { headers, data: { status } });
      expect(s.ok(), `set ${status} failed: ${s.status()}`).toBeTruthy();
    }
    return id;
  };

  const sales = await createSuite('Sales checks');
  const activeIds = [
    await createCase(sales, 'How many customers are there?', 'active'),
    await createCase(sales, 'Top 5 artists by sales', 'active'),
  ];
  await createCase(sales, 'Average invoice total per country', 'draft');
  await createCase(sales, 'Revenue by genre in 2009', 'archived');
  // A suite holding only a draft has nothing runnable and must not be offered.
  const ideas = await createSuite('Draft ideas');
  await createCase(ideas, 'Which employee closed the most invoices?', 'draft');

  // Capture what the modal sends without starting a real (LLM-backed) run.
  const sent: string[][] = [];
  await page.route('**/api/tests/runs', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    sent.push(route.request().postDataJSON().case_ids);
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
  });

  await page.goto(`/agents/${agentId}/evals`, { waitUntil: 'commit' });

  // Leave a search term behind in the Tests tab — it must not narrow the run.
  await page.getByRole('button', { name: 'Tests', exact: true }).click({ timeout: 45000 });
  await page.getByPlaceholder('Search tests...').fill('customers');
  await page.getByRole('button', { name: 'Test Runs', exact: true }).click();

  await page.getByRole('button', { name: 'Run evals', exact: true }).click();
  await expect(page.getByText('All suites (2 cases)')).toBeVisible();
  await expect(page.getByText('2 draft or archived cases not included')).toBeVisible();
  const runButton = page.getByRole('button', { name: 'Run 2 evals' });
  await expect(runButton).toBeEnabled();

  await page.getByText('All suites (2 cases)').click();
  await expect(page.getByRole('option', { name: 'Sales checks (2)' })).toBeVisible();
  await expect(page.getByRole('option', { name: /Draft ideas/ })).toHaveCount(0);
  await page.getByText('All suites (2 cases)').click();

  await runButton.click();
  await expect.poll(() => sent.length).toBe(1);
  expect([...sent[0]].sort()).toEqual([...activeIds].sort());
});
