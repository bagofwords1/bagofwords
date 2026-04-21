// Dedicated Playwright config for the Phase 8 locale sweep.
//
// The main playwright.config.ts attaches a globalSetup that signs up an
// admin user — that path is one-shot (registration is disabled after the
// first admin exists) and has nothing to do with locale checks. The i18n
// suite is unauthenticated-only, so we give it its own config without
// globalSetup. Run with:
//   npx playwright test --config=playwright.i18n.config.ts
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/i18n',
  timeout: 30 * 1000,
  retries: 1,
  fullyParallel: true,
  use: {
    headless: true,
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000',
    trace: 'retain-on-failure',
    storageState: { cookies: [], origins: [] },
  },
});
