import { test, expect } from '@playwright/test';
import * as fs from 'fs';

test('pending-review suggestion UI (Discard + Run eval)', async ({ page }) => {
  fs.mkdirSync('screenshots', { recursive: true });
  await page.goto('/users/sign-in');
  await page.waitForSelector('#email', { state: 'visible', timeout: 30000 });
  await page.fill('#email', 'sandbox@bow.dev');
  await page.fill('#password', 'Sandbox123!');
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 });
  await page.goto('/agents');
  await page.waitForLoadState('networkidle').catch(() => {});
  await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toBeVisible({ timeout: 20000 });

  // Expand the Pending review group via the top badge.
  await page.getByText(/\d+ pending/).first().click();
  await page.waitForTimeout(1000);
  await page.screenshot({ path: 'screenshots/sugg-1-overview.png', fullPage: true });

  // Click instruction rows until the right pane shows "Suggested changes".
  const rows = page.getByText(/^ACL:/);
  const n = Math.min(await rows.count(), 24);
  let found = false;
  for (let i = 0; i < n; i++) {
    await rows.nth(i).click().catch(() => {});
    await page.waitForTimeout(500);
    if (await page.getByText('Suggested changes').isVisible({ timeout: 800 }).catch(() => false)) {
      found = true;
      break;
    }
  }
  expect(found, 'found an instruction with a pending suggestion').toBeTruthy();
  await page.waitForTimeout(600);
  await page.screenshot({ path: 'screenshots/sugg-3-detail-suggested.png', fullPage: true });

  // Open the suggestion card → diff + Run-eval strip + Discard.
  const card = page.getByText(/AI suggestion|Proposed/).first();
  await expect(card).toBeVisible({ timeout: 8000 });
  await card.click();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'screenshots/sugg-4-diff-runeval.png', fullPage: true });
});
