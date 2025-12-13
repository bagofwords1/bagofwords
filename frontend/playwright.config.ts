// playwright.config.ts
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 60 * 1000,
  retries: 1,
  
  projects: [
    // 1. Setup - creates admin user
    {
      name: 'setup',
      testMatch: /global\.setup\.ts/,
    },

    // 2. Onboarding - admin completes onboarding
    {
      name: 'onboarding',
      testMatch: /onboarding\/.*\.spec\.ts/,
      dependencies: ['setup'],
      use: {
        storageState: 'tests/config/admin.json',
      },
    },

    // 3a. Members - invite + member signup (depends on onboarding)
    // MUST run sequentially: invite first, then signup
    {
      name: 'members',
      testMatch: /members\/.*\.spec\.ts/,
      dependencies: ['onboarding'],
      fullyParallel: false,  // Sequential within this project
    },

    // 3b. Features - reports, instructions, etc. (PARALLEL with members)
    {
      name: 'features',
      testMatch: /(reports|instructions|catalog|monitoring|evals|settings|home|data_sources|auth)\/.*\.spec\.ts/,
      dependencies: ['onboarding'],
      use: {
        storageState: 'tests/config/admin.json',
      },
    },

    // 4. Visibility - tests that need BOTH users to exist
    {
      name: 'visibility',
      testMatch: /visibility\/.*\.spec\.ts/,
      dependencies: ['members'],
    },
  ],

  use: {
    headless: true,
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
  },

  // Global setup creates admin user
  globalSetup: './tests/config/global.setup.ts',
});
