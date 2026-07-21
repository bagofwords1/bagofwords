// Standalone Playwright config for the MCP context-forwarding E2E.
// Self-contained (does its own 0→1 signup) so it needs no setup/onboarding
// project dependencies. Run with:
//   npx playwright test --config=pw.mcp.config.ts
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/mcp_forwarding',
  timeout: 180 * 1000,
  retries: 0,
  workers: 1,
  reporter: [['list']],
  use: {
    headless: true,
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000',
    trace: 'retain-on-failure',
    screenshot: 'off',
    // The sandbox ships chromium build 1194; this Playwright pins 1193. Point at
    // the installed full chrome instead of re-downloading.
    launchOptions: {
      executablePath: process.env.PW_CHROME || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
    },
  },
})
