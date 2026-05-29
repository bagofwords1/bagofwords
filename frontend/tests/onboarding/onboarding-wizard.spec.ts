import { test, expect } from '@playwright/test';

// Get OpenAI API key from environment
const OPENAI_API_KEY = process.env.OPENAI_API_KEY_TEST;

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

    // If still on any onboarding page, click "Skip onboarding" to dismiss it.
    // The button persists `dismissed=true` server-side via a PUT and then does
    // a client-side router.push('/'). That in-SPA redirect can be racy/slow in
    // dev-mode CI, so we don't depend on it here: we tolerate a timeout on the
    // immediate navigation and instead verify via a fresh navigation below,
    // which re-reads the (now persisted) dismissed state from the backend.
    if (page.url().includes('/onboarding')) {
      const skipButton = page.getByRole('button', { name: 'Skip onboarding' });
      await expect(skipButton).toBeVisible({ timeout: 15000 });
      await skipButton.click();
      await page
        .waitForURL((url) => !url.pathname.includes('/onboarding'), { timeout: 15000 })
        .catch(() => { /* fall through to fresh-navigation verification */ });
    }

    // Verify access to the app with a fresh navigation. Once onboarding is
    // dismissed/completed server-side, the middleware no longer redirects to
    // /onboarding. Poll to absorb dev-server slowness and any redirect lag.
    await expect(async () => {
      await page.goto('/');
      await page.waitForLoadState('domcontentloaded');
      expect(page.url()).not.toContain('/onboarding');
    }).toPass({ timeout: 30000 });

    // Save the final auth state (admin is now onboarded)
    await page.context().storageState({ path: 'tests/config/admin.json' });
    // Also save as auth.json for backwards compatibility
    await page.context().storageState({ path: 'tests/config/auth.json' });
  });
});

