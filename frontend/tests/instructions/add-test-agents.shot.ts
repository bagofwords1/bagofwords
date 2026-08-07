import { test, expect } from '@playwright/test';
import * as fs from 'fs';

// Verifies the test-case modal renames "Data sources" -> "Agents".
// (Default-selecting the in-context agent is wired via the modal's `agentId`
// prop on the agent-panel entry point; DataSourceSelector resolves it by id.)
test('Add Test Case modal uses the "Agents" label', async ({ page }) => {
  fs.mkdirSync('screenshots', { recursive: true });
  await page.goto('/users/sign-in');
  await page.waitForSelector('#email', { state: 'visible', timeout: 30000 });
  await page.fill('#email', 'sandbox@bow.dev');
  await page.fill('#password', 'Sandbox123!');
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 });

  // The standalone /evals page is gone. Its panel — the same tabs and buttons —
  // is reached by opening Global Evals in the Agents explorer (the LABEL opens
  // the runs panel; the chevron would expand the suite tree instead).
  await page.goto('/agents', { waitUntil: 'commit' });
  await page.waitForTimeout(2000);
  await page.getByText('Global Evals').first().click();
  await page.waitForTimeout(2000);

  // Open the Add Test Case modal.
  await page.getByRole('button', { name: /Add New Test/i }).first().click({ timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(1200);
  await expect(page.getByText('Add Test Case')).toBeVisible({ timeout: 8000 });
  await expect(page.getByText('Agents', { exact: true }).first()).toBeVisible({ timeout: 8000 });
  await page.screenshot({ path: 'screenshots/sl-8-add-test-agents.png', fullPage: true });
});
