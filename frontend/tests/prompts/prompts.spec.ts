// tests/prompts/prompts.spec.ts
//
// Live UI smoke test for the Prompt Catalog + Subscriptions feature.
//
// This spec is intentionally self-authenticating: it logs in through the
// sign-in form inside the test rather than relying on tests/config/global.setup.ts
// (which registers via the UI form and only works when the backend runs with
// local auth enabled, e.g. configs/bow-config.sandbox.yaml). Set the credentials
// of an existing org member via env, otherwise it falls back to the verification
// user seeded during Loop E.
//
//   PROMPTS_TEST_EMAIL / PROMPTS_TEST_PASSWORD
//
// Run:  npx playwright test tests/prompts/prompts.spec.ts --project=prompts

import { test, expect, Page } from '@playwright/test';

const EMAIL = process.env.PROMPTS_TEST_EMAIL || 'verify@bow.dev';
const PASSWORD = process.env.PROMPTS_TEST_PASSWORD || 'VerifyPass123!';

async function login(page: Page) {
  await page.goto('/users/sign-in', { waitUntil: 'networkidle' });
  await page.waitForSelector('#email', { state: 'visible', timeout: 30000 });
  await page.fill('#email', EMAIL);
  await page.fill('#password', PASSWORD);
  await page.click('button[type=submit]');
  await page.waitForTimeout(5000);

  // A fresh org with no data source / model lands on /onboarding — skip it.
  if (page.url().includes('/onboarding')) {
    const skip = page.locator('text=Skip onboarding');
    if (await skip.count()) {
      await skip.first().click();
      await page.waitForTimeout(2000);
    }
  }
}

async function gotoCatalog(page: Page) {
  await page.goto('/prompts', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  if (page.url().includes('/onboarding')) {
    const skip = page.locator('text=Skip onboarding');
    if (await skip.count()) await skip.first().click();
    await page.goto('/prompts', { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
  }
}

test.describe('Prompt Catalog', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await gotoCatalog(page);
  });

  test('catalog page renders with header, tabs, sort controls', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Prompt Catalog' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Catalog' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'My Subscriptions' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Top', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Recent', exact: true })).toBeVisible();
    // "Starters only" filter present
    await expect(page.getByText('Starters only')).toBeVisible();
  });

  test('catalog renders at least one prompt card with action buttons', async ({ page }) => {
    // At least one "Try now" + "Subscribe" pair indicates a rendered card.
    await expect(page.getByRole('button', { name: 'Try now' }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: 'Subscribe' }).first()).toBeVisible();
  });

  test('opening Subscribe modal shows channel select and run-mode toggle', async ({ page }) => {
    await page.getByRole('button', { name: 'Subscribe' }).first().click();

    // Modal header
    await expect(page.getByText(/^Subscribe to/)).toBeVisible({ timeout: 8000 });

    // Schedule picker + cron preset buttons
    await expect(page.getByText('Schedule', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Daily', exact: true })).toBeVisible();

    // Channel select with all four delivery channels
    const channelSelect = page.locator('.fixed select, [role="dialog"] select').last();
    const options = await channelSelect.locator('option').allTextContents();
    expect(options).toEqual(
      expect.arrayContaining(['Microsoft Teams', 'Slack', 'AI Mailbox', 'Email (SMTP)']),
    );

    // Run-mode toggle
    await expect(page.getByRole('button', { name: 'Append to one report' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'New report each run' })).toBeVisible();
  });
});
