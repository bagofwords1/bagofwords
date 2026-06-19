import { test, expect } from '@playwright/test';
import * as fs from 'fs';

// Captures the new per-agent "Self Learning" modal on the agent page
// (KnowledgeExplorer). Logs in as the seeded sandbox admin.
test('Self Learning per-agent automation modal', async ({ page }) => {
  fs.mkdirSync('screenshots', { recursive: true });

  // --- sign in ---
  await page.goto('/users/sign-in');
  await page.waitForSelector('#email', { state: 'visible', timeout: 30000 });
  await page.fill('#email', 'sandbox@bow.dev');
  await page.fill('#password', 'Sandbox123!');
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 });

  // --- open the agent page (Knowledge Explorer) ---
  await page.goto('/agents');
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(1500);

  // Click the demo agent in the left tree to open its overview.
  await page.getByText('Music Store', { exact: false }).first().click();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'screenshots/sl-1-agent-overview.png', fullPage: true });

  // --- open the Self Learning modal ---
  const btn = page.getByRole('button', { name: /Self Learning/i }).first();
  await expect(btn).toBeVisible({ timeout: 10000 });
  await btn.click();
  await page.waitForTimeout(1200);
  await expect(page.getByText('Auto-approve suggestions?')).toBeVisible({ timeout: 8000 });
  await page.screenshot({ path: 'screenshots/sl-2-modal-tree.png', fullPage: true });

  // --- expand Advanced to show the failure handling ---
  await page.getByRole('button', { name: /^Advanced$/ }).click().catch(() => {});
  await page.waitForTimeout(600);
  await page.screenshot({ path: 'screenshots/sl-3-modal-advanced.png', fullPage: true });

  // --- toggle Auto-approve to show the summary collapse the eval branch ---
  await page.getByText('Auto-approve suggestions?').click().catch(() => {});
  await page.waitForTimeout(400);
  await page.screenshot({ path: 'screenshots/sl-4-modal-autoapprove.png', fullPage: true });
});
