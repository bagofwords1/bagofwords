import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './tests/tables_canvas', testMatch: 'tables-canvas.spec.ts',
  timeout: 90_000, workers: 1, retries: 0,
  outputDir: process.env.BOW_ERD_RESULTS || '/tmp/bow-tables-erd-run/results',
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3107',
    viewport: { width: 1440, height: 900 },
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE },
    video: 'on', trace: 'retain-on-failure', screenshot: 'only-on-failure',
  },
})
