// Dedicated Playwright config for the live Entra SSO + in-app OBO tests.
// No default globalSetup (it creates an admin via password signup, disabled under
// auth.mode=sso_only). Instead a `setup` project performs the real Entra login,
// persists the session (storageState) and the bearer token for API-level checks.
import { defineConfig } from '@playwright/test';

const common = {
  headless: true,
  // The sandbox routes outbound HTTPS through a TLS-intercepting proxy whose CA
  // Chromium doesn't trust; without this, login.microsoftonline.com fails with
  // ERR_CERT_AUTHORITY_INVALID.
  ignoreHTTPSErrors: true,
  baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000',
  trace: 'retain-on-failure' as const,
  screenshot: 'only-on-failure' as const,
};

export default defineConfig({
  testDir: './tests/entra',
  timeout: 180 * 1000,
  retries: 0,
  workers: 1,
  reporter: [['list']],
  projects: [
    // Standalone proof that the raw SSO login works (no dependency).
    { name: 'login', testMatch: '**/entra-login.spec.ts', use: common },
    // Shared setup: real Entra login -> storageState + bearer token on disk.
    { name: 'setup', testMatch: '**/auth.setup.ts', use: common },
    // In-app OBO flows reuse the authenticated session.
    {
      name: 'inapp',
      testMatch: '**/obo-inapp.spec.ts',
      dependencies: ['setup'],
      use: { ...common, storageState: 'tests/entra/.auth/demo1.json' },
    },
  ],
});
