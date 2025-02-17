// global.setup.ts
import { chromium, FullConfig } from '@playwright/test';

const TEST_USER = {
  name: 'Yochay',
  email: 'yochay49@gmail.com',
  password: '1234123!'
};

const TEST_ORGANIZATION = {
  name: 'TestOrg49'
};

async function globalSetup(config: FullConfig) {

  const { baseURL } = config.projects[0].use;
  const browser = await chromium.launch();
  const page = await browser.newPage();
  
  // Sign up once
  await page.goto(`${baseURL}/users/sign-up`);
  await page.fill('#name', TEST_USER.name);
  await page.fill('#email', TEST_USER.email);
  await page.fill('#password', TEST_USER.password);
  await page.click('button[type="submit"]');

  // Wait for navigation to /organizations/new after signup
  await page.waitForURL('**/organizations/new');
  await page.waitForLoadState('networkidle');

  // Fill out organization form
  await page.waitForSelector('input[name="name"]');
  
  await page.fill('input[name="name"]', TEST_ORGANIZATION.name);
  await page.click('button[type="submit"]');

  // Wait for organization creation and cookie to be set
  await page.waitForLoadState('networkidle');
  
  await page.waitForFunction(() => {
    return document.cookie.includes('organization=');
  }, { timeout: 5000 });

  // Save authentication state for reuse
  await page.context().storageState({ path: 'tests/config/auth.json' });
  await browser.close();
}

export default globalSetup;
