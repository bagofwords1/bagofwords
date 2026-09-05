import { test, expect } from '../fixtures/auth';

test.describe('Shared page visibility (both admin and member)', () => {

  test('admin can access reports page', async ({ adminPage }) => {
    await adminPage.goto('/reports');
    await adminPage.waitForLoadState('domcontentloaded');

    await expect(adminPage.getByRole('heading', { name: 'Reports', exact: true }))
      .toBeVisible({ timeout: 10000 });
  });

  test('member can access reports page', async ({ memberPage }) => {
    await memberPage.goto('/reports');
    await memberPage.waitForLoadState('domcontentloaded');

    await expect(memberPage.getByRole('heading', { name: 'Reports', exact: true }))
      .toBeVisible({ timeout: 10000 });
  });

  test('admin can access instructions page', async ({ adminPage }) => {
    await adminPage.goto('/agents');
    await adminPage.waitForLoadState('domcontentloaded');

    await expect(adminPage.getByRole('heading', { name: 'Agents', exact: true }))
      .toBeVisible({ timeout: 10000 });
  });

  test('member can access instructions page', async ({ memberPage }) => {
    await memberPage.goto('/agents');
    await memberPage.waitForLoadState('domcontentloaded');

    await expect(memberPage.getByRole('heading', { name: 'Agents', exact: true }))
      .toBeVisible({ timeout: 10000 });
  });

  // Queries live in the /agents tree now; /queries redirects there.
  test('admin can reach the queries catalog', async ({ adminPage }) => {
    await adminPage.goto('/queries');
    await adminPage.waitForLoadState('domcontentloaded');

    await expect(adminPage).toHaveURL(/\/agents$/, { timeout: 10000 });
    await expect(adminPage.getByRole('heading', { name: 'Agents', exact: true }))
      .toBeVisible({ timeout: 10000 });
  });

  test('member can reach the queries catalog', async ({ memberPage }) => {
    await memberPage.goto('/queries');
    await memberPage.waitForLoadState('domcontentloaded');

    await expect(memberPage).toHaveURL(/\/agents$/, { timeout: 10000 });
    await expect(memberPage.getByRole('heading', { name: 'Agents', exact: true }))
      .toBeVisible({ timeout: 10000 });
  });
});

