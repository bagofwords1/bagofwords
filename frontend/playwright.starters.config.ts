// Standalone Playwright config for the agent-starters spec. Mirrors the base
// config but drops globalSetup / the setup->onboarding project chain, since the
// spec signs in with a pre-seeded admin user and seeds its own data via the API.
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/agent-starters',
  timeout: 90 * 1000,
  retries: 0,
  use: {
    headless: true,
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000',
    trace: 'retain-on-failure',
  },
});
