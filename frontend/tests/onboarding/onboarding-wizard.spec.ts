import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

// Get OpenAI API key from environment
const OPENAI_API_KEY = process.env.OPENAI_API_KEY_TEST;

// Dismiss the onboarding wizard and wait until we've actually left /onboarding.
//
// This is resilient to two CI-only races that make a single click flaky:
//   1. SSR hydration lag — the "Skip onboarding" button renders (and reports
//      visible) before its Vue @click handler is attached, so an early click is
//      a no-op. We wait for the network to go idle first, and re-click if the
//      first click didn't take.
//   2. A brief middleware bounce back to /onboarding right after dismissal.
// Returns once the URL no longer contains /onboarding (or throws after retries).
async function dismissOnboarding(page: Page, attempts = 5): Promise<void> {
  for (let i = 0; i < attempts; i++) {
    if (!page.url().includes('/onboarding')) return;

    // Let client-side hydration settle so the click handler is bound.
    await page.waitForLoadState('networkidle').catch(() => {});

    const skipButton = page.getByRole('button', { name: 'Skip onboarding' });
    if (!(await skipButton.isVisible({ timeout: 15000 }).catch(() => false))) {
      // No skip button — either already off onboarding, or page still settling.
      if (!page.url().includes('/onboarding')) return;
      await page.waitForTimeout(1000);
      continue;
    }

    await skipButton.click();
    const left = await page
      .waitForURL((url) => !url.pathname.includes('/onboarding'), { timeout: 8000 })
      .then(() => true)
      .catch(() => false);
    if (left) return;
    // Click didn't take (hydration race) or we bounced back — retry.
  }
  expect(page.url(), 'should have left /onboarding after dismissing').not.toContain('/onboarding');
}


test.describe('Onboarding Wizard', () => {
  
  test('step 1: welcome page - proceed to LLM setup', async ({ page }) => {
    await page.goto('/onboarding');
    await page.waitForLoadState('domcontentloaded');
    
    // Verify welcome message is displayed (increase timeout for CI)
    await expect(page.getByText('Getting started is quick')).toBeVisible({ timeout: 30000 });
    
    // Click Next to proceed to LLM configuration
    await page.getByRole('button', { name: 'Next' }).click();
    
    // Verify navigation to LLM page
    await page.waitForURL('**/onboarding/llm', { timeout: 15000 });
  });

  test('step 2: configure OpenAI LLM provider', async ({ page }) => {
    // Skip if no API key provided
    test.skip(!OPENAI_API_KEY, 'OPENAI_API_KEY environment variable not set');
    
    await page.goto('/onboarding/llm');
    await page.waitForLoadState('domcontentloaded');
    
    // Wait for provider options to load
    await expect(page.getByText('OpenAI', { exact: true })).toBeVisible({ timeout: 30000 });
    
    // Click on OpenAI provider option (exact match to avoid Azure OpenAI)
    await page.getByText('OpenAI', { exact: true }).click();
    await page.waitForLoadState('domcontentloaded');
    
    // Wait for the configuration form to appear
    await page.waitForTimeout(1000);
    
    // Fill in the Name field (first input)
    const nameInput = page.locator('input[type="text"]').first();
    await nameInput.clear();
    await nameInput.fill('OpenAI Test Provider');
    
    // Fill in the API Key field (second input)
    const apiKeyInput = page.locator('input[type="text"]').nth(1);
    await apiKeyInput.fill(OPENAI_API_KEY!);
    
    // Verify GPT models are displayed (models are auto-selected when provider is chosen)
    await expect(page.getByText(/GPT/i).first()).toBeVisible({ timeout: 5000 });
    
    // Click Test Connection button
    const testButton = page.getByRole('button', { name: 'Test Connection' });
    await expect(testButton).toBeEnabled({ timeout: 5000 });
    await testButton.click();
    
    // Wait for connection test to complete (success message in green)
    await expect(page.getByText(/successfully connected/i)).toBeVisible({ timeout: 30000 });
    
    // Save and Next button should now be enabled
    const saveButton = page.getByRole('button', { name: /save and next/i });
    await expect(saveButton).toBeEnabled({ timeout: 5000 });
    await saveButton.click();
    
    // Wait for navigation to data source page
    await page.waitForURL('**/onboarding/data', { timeout: 10000 });
  });

  test('step 3: connect sample database', async ({ page }) => {
    await page.goto('/onboarding/data');
    await page.waitForLoadState('domcontentloaded');
    
    // Wait for data source options to load
    await page.waitForTimeout(2000);
    
    // Look for and click a sample database (Chinook)
    const sampleButton = page.getByRole('button', { name: /chinook/i });
    
    if (await sampleButton.isVisible({ timeout: 10000 }).catch(() => false)) {
      await sampleButton.click();
      
      // Wait for installation and navigation to schema page
      await page.waitForURL('**/schema', { timeout: 30000 });
      
      // On schema page, wait for it to load
      await page.waitForLoadState('domcontentloaded');
      
      // Look for continue/save button
      const continueButton = page.getByRole('button', { name: /continue|save|next/i }).first();
      if (await continueButton.isVisible({ timeout: 10000 }).catch(() => false)) {
        await continueButton.click();
        await page.waitForLoadState('domcontentloaded');
      }
    } else {
      // If no sample DB, look for any data source type to verify page works
      const dsTypes = page.locator('button').filter({ hasText: /sqlite|postgres|duckdb|mysql/i }).first();
      await expect(dsTypes).toBeVisible({ timeout: 10000 });
    }
  });

  test('step 4: complete onboarding and verify access to app', async ({ page }) => {
    // Navigate directly to /onboarding
    // If already completed, the onboarding middleware redirects to /
    await page.goto('/onboarding');
    await page.waitForLoadState('domcontentloaded');

    // Wait for page to settle (client-side hydration + potential redirects)
    await page.waitForTimeout(5000);

    // If still on any onboarding page, skip it to complete the flow.
    // dismissOnboarding handles the hydration/bounce races (retries the click).
    await dismissOnboarding(page);

    // Verify we're not on onboarding
    expect(page.url()).not.toContain('/onboarding');

    // Double-check: navigate to a feature page to verify no redirect
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(3000);

    // If redirected again, dismiss once more.
    await dismissOnboarding(page);

    expect(page.url()).not.toContain('/onboarding');

    // Save the final auth state (admin is now onboarded)
    await page.context().storageState({ path: 'tests/config/admin.json' });
    // Also save as auth.json for backwards compatibility
    await page.context().storageState({ path: 'tests/config/auth.json' });
  });
});

