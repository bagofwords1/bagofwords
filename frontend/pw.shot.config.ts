import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests',
  timeout: 90 * 1000,
  retries: 0,
  projects: [{ name: 'shot', testMatch: '**/*.shot.ts' }],
  use: { headless: true, baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000' },
});
