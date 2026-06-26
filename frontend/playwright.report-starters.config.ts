// Standalone Playwright config for the in-report conversation-starters spec.
// Mirrors playwright.starters.config.ts: no globalSetup / onboarding chain — the
// spec signs in with a pre-seeded admin user and reads its seed from env.json.
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/report-agent-starters',
  timeout: 240 * 1000,
  retries: 0,
  use: {
    headless: true,
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000',
    trace: 'retain-on-failure',
  },
});
