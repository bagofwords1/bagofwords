// Standalone Playwright config for the embedded-chat feedback loop
// (tests/embed/*.spec.ts). Needs the sandbox from docs/feedback-loops/
// embedded-chat.md: BOW stack on :3000/:8000, echo MCP on :3333, stub LLM on
// :9099 and the ERP host on :4173. Run with:
//   npx playwright test --config=pw.embed.config.ts
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/embed',
  timeout: 240 * 1000,
  retries: 0,
  workers: 1,
  reporter: [['list']],
  use: {
    headless: true,
    baseURL: process.env.ERP_BASE_URL || 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
    screenshot: 'off',
    viewport: { width: 1360, height: 860 },
    // Cloud sandboxes ship a newer Chromium than this Playwright pins.
    launchOptions: {
      executablePath: process.env.PW_CHROME || '/opt/pw-browsers/chromium',
    },
  },
})
