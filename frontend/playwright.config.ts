// playwright.config.ts
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 30 * 1000,
  retries: 1,
  globalSetup: './tests/config/global.setup.ts',
  use: {
    headless: true,
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
    storageState: 'tests/config/auth.json', // Reuse the authenticated state
  },
});