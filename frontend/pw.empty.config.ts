import { defineConfig, devices } from '@playwright/test'

// Screenshot-only config for the empty-state illustrations on /evals,
// /scheduled-tasks and /queries. No globalSetup / onboarding dependency so the
// pages stay genuinely empty. Run manually:
//   npx playwright test tests/empty-states/empty-states.shot.ts --config=pw.empty.config.ts
export default defineConfig({
  testDir: './tests/empty-states',
  testMatch: '**/*.shot.ts',
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000',
    viewport: { width: 1440, height: 900 },
    headless: true,
  },
  projects: [
    { name: 'shots', use: { ...devices['Desktop Chrome'] } },
  ],
})
