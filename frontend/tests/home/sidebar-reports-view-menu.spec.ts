import { test, expect } from '../fixtures/feature-test';
import type { Page } from '@playwright/test';

// Sidebar menus (Group and sort, report row ⋯, project ⋯) used a full-screen
// transparent backdrop to detect outside clicks. That backdrop ate the next
// click: with a menu open, pressing another ⋯ or a link only closed the menu
// and needed a second click. Outside presses must now close the menu AND
// reach their target in the same click (useDismissOnOutsidePress).

async function apiHeaders(page: Page) {
  const cookies = await page.context().cookies();
  const rawToken = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token')?.value;
  expect(rawToken, 'auth token cookie should exist in the admin storage state').toBeTruthy();
  const bearer = decodeURIComponent(rawToken!);
  const auth = bearer.startsWith('Bearer') ? bearer : `Bearer ${bearer}`;
  const whoami = await page.request.get('/api/users/whoami', { headers: { Authorization: auth } });
  expect(whoami.ok()).toBeTruthy();
  const orgId = (await whoami.json()).organizations?.[0]?.id;
  return { Authorization: auth, 'X-Organization-Id': orgId };
}

async function createReport(page: Page, headers: Record<string, string>, title: string) {
  const res = await page.request.post('/api/reports', { headers, data: { title } });
  expect(res.ok(), `create report failed: ${res.status()}`).toBeTruthy();
  return (await res.json()).id as string;
}

const tag = () => Date.now().toString(36);
const reportRow = (page: Page, title: string) => page.locator('li.group\\/report', { hasText: title });
const rowMenuItem = (page: Page) => page.getByRole('button', { name: 'Rename' });

async function openRowMenu(page: Page, title: string) {
  const row = reportRow(page, title);
  await row.hover();
  await row.getByRole('button', { name: 'Report actions' }).click();
}

test('an outside click closes the group/sort menu and still reaches its target', async ({ page }) => {
  const headers = await apiHeaders(page);
  const title = `View menu ${tag()}`;
  const id = await createReport(page, headers, title);
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
    await openRowMenu(page, title);
    await expect(groupByRow).toBeHidden();
    await expect(rowMenuItem(page)).toBeVisible();
  } finally {
    await page.request.delete(`/api/reports/${id}`, { headers });
  }
});

test('report row menu: another ⋯ opens in one click, same ⋯ toggles closed', async ({ page }) => {
  const headers = await apiHeaders(page);
  const t = tag();
  const a = `Row menu A ${t}`;
  const b = `Row menu B ${t}`;
  const ids = [await createReport(page, headers, a), await createReport(page, headers, b)];
  try {
    await page.goto('/reports', { waitUntil: 'commit' });
    await expect(reportRow(page, b)).toBeVisible({ timeout: 20000 });

    // Open A's menu, then ONE click on B's ⋯ swaps to B's menu.
    await openRowMenu(page, a);
    await expect(rowMenuItem(page)).toBeVisible();
    await openRowMenu(page, b);
    await expect(rowMenuItem(page)).toHaveCount(1);
    const bBtn = reportRow(page, b).getByRole('button', { name: 'Report actions' });
    await expect(bBtn).toHaveClass(/opacity-100/);

    // The same ⋯ again closes it (no reopen flicker).
    await bBtn.click();
    await expect(rowMenuItem(page)).toBeHidden();

    // With a menu open, ONE click on another report's link navigates.
    await openRowMenu(page, a);
    await expect(rowMenuItem(page)).toBeVisible();
    await reportRow(page, b).locator('a').click();
    await expect(page).toHaveURL(new RegExp(`/reports/${ids[1]}`));
    await expect(rowMenuItem(page)).toBeHidden();
  } finally {
    for (const id of ids) await page.request.delete(`/api/reports/${id}`, { headers });
  }
});

test('project menu: one click on a report ⋯ closes it and opens the report menu', async ({ page }) => {
  const headers = await apiHeaders(page);
  const t = tag();
  const projectName = `Menu project ${t}`;
  const title = `Project menu R ${t}`;
  const pRes = await page.request.post('/api/projects', { headers, data: { name: projectName } });
  expect(pRes.ok(), `create project failed: ${pRes.status()}`).toBeTruthy();
  const projectId = (await pRes.json()).id;
  const reportId = await createReport(page, headers, title);
  try {
    await page.goto('/reports', { waitUntil: 'commit' });
    const projectRow = page.locator('li', { hasText: projectName }).last();
    await expect(projectRow).toBeVisible({ timeout: 20000 });
    await projectRow.hover();
    await projectRow.getByRole('button', { name: 'Project actions' }).click();
    // Project menu shows Rename too; a report menu adds "Move to project".
    await expect(rowMenuItem(page)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Move to project' })).toBeHidden();

    // A single raw press where the user aims (no actionability retries, which
    // would wait out a click-eating backdrop and hide the bug).
    const box = await reportRow(page, title).getByRole('button', { name: 'Report actions' }).boundingBox();
    expect(box).toBeTruthy();
    await page.mouse.click(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await expect(page.getByRole('button', { name: 'Move to project' })).toBeVisible();
    await expect(rowMenuItem(page)).toHaveCount(1);
  } finally {
    await page.request.delete(`/api/reports/${reportId}`, { headers });
    await page.request.delete(`/api/projects/${projectId}`, { headers });
  }
});
