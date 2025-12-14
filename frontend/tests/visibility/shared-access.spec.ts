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
    await adminPage.goto('/instructions');
    await adminPage.waitForLoadState('domcontentloaded');

    await expect(adminPage.getByRole('heading', { name: 'Instructions', exact: true }))
      .toBeVisible({ timeout: 10000 });
  });

  test('member can access instructions page', async ({ memberPage }) => {
    await memberPage.goto('/instructions');
    await memberPage.waitForLoadState('domcontentloaded');

    await expect(memberPage.getByRole('heading', { name: 'Instructions', exact: true }))
      .toBeVisible({ timeout: 10000 });
  });

  test('admin can access catalog page', async ({ adminPage }) => {
    await adminPage.goto('/catalog');
    await adminPage.waitForLoadState('domcontentloaded');

    await expect(adminPage.getByRole('heading', { name: 'Catalog', exact: true }))
      .toBeVisible({ timeout: 10000 });
  });

  test('member can access catalog page', async ({ memberPage }) => {
    await memberPage.goto('/catalog');
    await memberPage.waitForLoadState('domcontentloaded');

    await expect(memberPage.getByRole('heading', { name: 'Catalog', exact: true }))
      .toBeVisible({ timeout: 10000 });
  });
});

