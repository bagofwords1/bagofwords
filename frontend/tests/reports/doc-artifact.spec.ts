import { test, expect } from '../fixtures/feature-test';

// Doc artifacts (mode='doc') — the public share page must render a shared
// document through DocViewer: markdown prose, GFM table, mermaid diagram,
// live {{viz:<uuid>}} chart embed, and multi-column layout — and must NOT
// fall back to the JSX iframe. All backend responses are intercepted so the
// spec is deterministic (no LLM, no seeding).

const REPORT_ID = 'doc-spec-report-1';
const DOC_ID = 'doc-spec-artifact-1';
const VIZ_ID = '6f0a3c9e-6a51-4c1e-9c8e-2b7f3a1d4e5f';
const QUERY_ID = 'doc-spec-query-1';

const MARKDOWN = `# Quarterly Revenue Review

## Findings

Revenue is concentrated in **Rock and Latin** genres (source: \`invoices.total\`, 2024).

| Genre | Revenue |
|-------|---------|
| Rock  | $826    |
| Latin | $382    |

{{viz:${VIZ_ID}}}

::: columns
Left column narrative.
::: col
Right column narrative.
:::

\`\`\`mermaid
graph TD; Spike[Purchase spike] --> Wave[Revenue wave]
\`\`\`

\`\`\`
quoted example: {{viz:00000000-0000-0000-0000-000000000000}}
\`\`\`
`;

test('shared doc renders markdown, table, viz, columns and mermaid via DocViewer', async ({ page }) => {
  await page.route(`**/api/r/${REPORT_ID}`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: REPORT_ID,
        title: 'Doc Spec Report',
        status: 'published',
        user: { id: 'user-1', name: 'Owner' },
        general: { bow_credit: false },
      }),
    })
  );
  await page.route(`**/api/r/${REPORT_ID}/artifacts`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        { id: DOC_ID, report_id: REPORT_ID, title: 'Quarterly Revenue Review', mode: 'doc', version: 1, status: 'completed', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
      ]),
    })
  );
  await page.route(`**/api/r/${REPORT_ID}/artifacts/${DOC_ID}`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: DOC_ID,
        report_id: REPORT_ID,
        user_id: 'user-1',
        organization_id: 'org-1',
        title: 'Quarterly Revenue Review',
        mode: 'doc',
        version: 1,
        status: 'completed',
        content: { markdown: MARKDOWN, visualization_ids: [VIZ_ID] },
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }),
    })
  );
  await page.route(`**/api/r/${REPORT_ID}/layouts**`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  );
  await page.route(`**/api/r/${REPORT_ID}/queries**`, (route) => {
    // Both /queries?artifact_id=... and /queries/{id}/step hit this glob —
    // dispatch on the URL so each gets its own payload.
    const url = route.request().url();
    if (url.includes(`/queries/${QUERY_ID}/step`)) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'step-1',
          status: 'success',
          data: {
            rows: [
              { genre: 'Rock', revenue: 826 },
              { genre: 'Latin', revenue: 382 },
            ],
            columns: [{ field: 'genre' }, { field: 'revenue' }],
          },
          data_model: { type: 'bar_chart', columns: [{ name: 'genre' }, { name: 'revenue' }] },
        }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          id: QUERY_ID,
          title: 'Revenue by Genre',
          visualizations: [
            { id: VIZ_ID, title: 'Revenue by Genre', view: { type: 'bar_chart' } },
          ],
        },
      ]),
    });
  });

  await page.goto(`/r/${REPORT_ID}`);
  await page.waitForLoadState('domcontentloaded');

  // DocViewer rendered the document (not the JSX iframe)
  await expect(page.locator('.bow-doc')).toBeVisible({ timeout: 20000 });
  await expect(page.getByRole('heading', { name: 'Quarterly Revenue Review' })).toBeVisible();
  await expect(page.locator('iframe[sandbox]')).toHaveCount(0);

  // Markdown table renders as a real table
  await expect(page.locator('.bow-doc-md table')).toBeVisible();
  await expect(page.getByRole('cell', { name: '$826' })).toBeVisible();

  // Live viz embed: the chart card mounts with the viz title (not a placeholder string)
  await expect(page.locator('.doc-viz')).toBeVisible();
  await expect(page.locator('.bow-doc')).not.toContainText(`{{viz:${VIZ_ID}}}`);

  // Multi-column grid renders both columns side by side
  await expect(page.locator('.doc-columns')).toBeVisible();
  await expect(page.getByText('Left column narrative.')).toBeVisible();
  await expect(page.getByText('Right column narrative.')).toBeVisible();

  // Mermaid renders an SVG diagram (or, at minimum, its fallback keeps the page alive)
  await expect(page.locator('.doc-mermaid')).toBeVisible();
  await expect(page.locator('.doc-mermaid svg')).toBeVisible({ timeout: 20000 });

  // The fenced example placeholder stays literal text (fence-aware parsing)
  await expect(page.locator('.bow-doc')).toContainText('quoted example: {{viz:00000000');
});
