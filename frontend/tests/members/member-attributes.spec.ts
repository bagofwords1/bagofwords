import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

// Admin-set member attributes (Settings → Members): add one through the
// editor, see it on the member's row, then remove it again. Runs against the
// admin's own row so it does not depend on the invite flow's ordering.
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function getTestUsers() {
  return JSON.parse(fs.readFileSync(path.join(__dirname, '../config/test-users.json'), 'utf-8'));
}

test('admin adds and removes a member attribute', async ({ browser }) => {
  const { admin } = getTestUsers();
  const context = await browser.newContext({ storageState: 'tests/config/admin.json' });
  const page = await context.newPage();

  await page.goto('/settings/members', { waitUntil: 'commit' });
  await page.waitForLoadState('domcontentloaded');
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible({ timeout: 30000 });

  const row = page.locator('tr').filter({ hasText: admin.email });
  await expect(row).toBeVisible({ timeout: 30000 });
  const value = `role_${Date.now()}`;

  await row.locator('[data-testid^="edit-attributes-"]').click();
  const modal = page.locator('[data-testid="member-attributes-modal"]');
  await expect(modal).toBeVisible();
  await modal.locator('[data-testid="attr-add"]').click();
  const rows = modal.locator('[data-testid^="attr-key-"]');
  const idx = (await rows.count()) - 1;
  await modal.locator(`[data-testid="attr-key-${idx}"]`).locator('input').or(modal.locator(`input[data-testid="attr-key-${idx}"]`)).first().fill('pg_role');
  await modal.locator(`[data-testid="attr-value-${idx}"]`).locator('input').or(modal.locator(`input[data-testid="attr-value-${idx}"]`)).first().fill(value);
  await modal.locator('[data-testid="save-attributes"]').click();
  await expect(page.getByText('Attributes saved').first()).toBeVisible({ timeout: 15000 });
  await expect(row.getByText(`pg_role: ${value}`)).toBeVisible();

  // An invalid name is refused before it reaches the server.
  await row.locator('[data-testid^="edit-attributes-"]').click();
  await expect(modal).toBeVisible();
  await modal.locator('[data-testid="attr-add"]').click();
  const bad = (await modal.locator('[data-testid^="attr-key-"]').count()) - 1;
  await modal.locator(`[data-testid="attr-key-${bad}"]`).locator('input').or(modal.locator(`input[data-testid="attr-key-${bad}"]`)).first().fill('has space');
  await expect(modal.locator('[data-testid="save-attributes"]')).toBeDisabled();

  // Remove both rows → the attribute is gone after save.
  while (await modal.getByRole('button', { name: 'Remove attribute' }).count()) {
    await modal.getByRole('button', { name: 'Remove attribute' }).first().click();
  }
  await modal.locator('[data-testid="save-attributes"]').click();
  await expect(page.getByText('Attributes saved').first()).toBeVisible({ timeout: 15000 });
  await expect(row.getByText(`pg_role: ${value}`)).toHaveCount(0);

  await context.close();
});
