// global.setup.ts
import { chromium, FullConfig } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

// ES Module equivalent of __dirname
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Test users - exported for use in other tests
export const TEST_ADMIN = {
  name: 'E2E Admin User',
  email: `e2e-admin-${Date.now()}@example.com`,
  password: 'TestPass123!'
};

export const TEST_MEMBER = {
  name: 'E2E Member User',
  email: `e2e-member-${Date.now()}@example.com`,
  password: 'TestPass123!'
};

async function globalSetup(config: FullConfig) {
  const { baseURL } = config.projects[0].use;
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  
  // Navigate and wait for page to be ready
  await page.goto(`${baseURL}/users/sign-up`);
  await page.waitForLoadState('networkidle');
  
  // Wait for form to be visible (page shows spinner until loaded)
  await page.waitForSelector('#name', { state: 'visible', timeout: 10000 });
  
  // Fill the form
  await page.fill('#name', TEST_ADMIN.name);
  await page.fill('#email', TEST_ADMIN.email);
  await page.fill('#password', TEST_ADMIN.password);
  
  // Submit and wait for either navigation or error
  await page.click('button[type="submit"]');
  
  // Wait for response - either navigation happens or error appears
  const result = await Promise.race([
    page.waitForURL((url) => !url.pathname.includes('/users/sign-up'), { timeout: 15000 })
      .then(() => 'navigated'),
    page.waitForSelector('.text-red-500', { timeout: 15000 })
      .then(() => 'error')
  ]);
  
  if (result === 'error') {
    const errorText = await page.locator('.text-red-500').textContent();
    await page.screenshot({ path: 'tests/config/signup-error.png' });
    throw new Error(`Sign-up failed: ${errorText}`);
  }
  
  await page.waitForLoadState('networkidle');

  // If redirected to onboarding, wait for it to load
  if (page.url().includes('/onboarding')) {
    await page.waitForSelector('text=Getting started is quick', { timeout: 10000 });
  }

  // Save authentication state as admin.json
  await page.context().storageState({ path: 'tests/config/admin.json' });
  
  // Also save as auth.json for backwards compatibility
  await page.context().storageState({ path: 'tests/config/auth.json' });
  
  // Save test user info for member tests to use
  const testUsersPath = path.join(__dirname, 'test-users.json');
  fs.writeFileSync(testUsersPath, JSON.stringify({
    admin: TEST_ADMIN,
    member: TEST_MEMBER
  }, null, 2));
  
  await browser.close();
}

export default globalSetup;
