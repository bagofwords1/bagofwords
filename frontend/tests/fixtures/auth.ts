import { test as base, Page } from '@playwright/test';

// Extended test with role-specific page fixtures
export const test = base.extend<{
  adminPage: Page;
  memberPage: Page;
}>({
  // Admin page - uses admin auth state
  adminPage: async ({ browser }, use) => {
    const context = await browser.newContext({
      storageState: 'tests/config/admin.json',
    });
    const page = await context.newPage();
    await use(page);
    await context.close();
  },

  // Member page - uses member auth state
  memberPage: async ({ browser }, use) => {
    const context = await browser.newContext({
      storageState: 'tests/config/member.json',
    });
    const page = await context.newPage();
    await use(page);
    await context.close();
  },
});

export { expect } from '@playwright/test';

