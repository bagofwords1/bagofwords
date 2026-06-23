import { test, expect } from '@playwright/test';
import * as fs from 'fs';

async function signIn(page) {
  await page.goto('/users/sign-in');
  await page.waitForSelector('#email', { state: 'visible', timeout: 30000 });
  await page.fill('#email', 'sandbox@bow.dev');
  await page.fill('#password', 'Sandbox123!');
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 });
}

// 1) Resolved-eval strip in the instruction detail view.
test('resolved-eval strip on an instruction', async ({ page }) => {
  fs.mkdirSync('screenshots', { recursive: true });
  await signIn(page);
  await page.goto('/agents');
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(1500);

  // Open the Music Store agent, then click one of its instructions.
  await page.getByText('Music Store', { exact: false }).first().click();
  await page.waitForTimeout(1200);
  // Click an instruction row in the tree (any of the seeded ones).
  const instr = page.getByText(/Best sellers|VIP customers|top X by/i).first();
  await instr.click().catch(() => {});
  await page.waitForTimeout(1500);
  // The strip text.
  await expect(page.getByText(/evals? resolved for this agent/i)).toBeVisible({ timeout: 8000 });
  await page.screenshot({ path: 'screenshots/sl-5-resolved-eval-strip.png', fullPage: true });
});

// 2) Clickable build badge on the evals Runs tab -> BuildExplorerModal.
test('eval run build badge opens BuildExplorerModal', async ({ page }) => {
  fs.mkdirSync('screenshots', { recursive: true });
  await signIn(page);
  await page.goto('/evals');
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(1500);

  // Switch to the "Test Runs" tab.
  await page.getByText('Test Runs', { exact: true }).first().click({ timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(2000);
  await page.screenshot({ path: 'screenshots/sl-6-evals-runs.png', fullPage: true });

  // Click the build badge (#<number>) to open the whole-build view.
  const badge = page.locator('button', { hasText: /#\d+/ }).first();
  await expect(badge).toBeVisible({ timeout: 8000 });
  await badge.click();
  await page.waitForTimeout(1800);
  await page.screenshot({ path: 'screenshots/sl-7-build-explorer.png', fullPage: true });
});
