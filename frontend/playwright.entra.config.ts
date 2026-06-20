// Dedicated Playwright config for the live Entra SSO end-to-end test.
// No globalSetup (the default one creates an admin via password signup, which
// is disabled under auth.mode=sso_only). Single worker, generous timeouts for
// the real Microsoft login round-trip.
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/entra',
  timeout: 180 * 1000,
  retries: 0,
  workers: 1,
  reporter: [['list']],
  use: {
    headless: true,
    // The sandbox routes outbound HTTPS through a TLS-intercepting proxy whose CA
    // Chromium doesn't trust; without this, navigating to login.microsoftonline.com
    // fails with ERR_CERT_AUTHORITY_INVALID.
    ignoreHTTPSErrors: true,
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
});
