// UI verification for shared-artifact viewer runs (feedback loop:
// docs/feedback-loops/shared-artifact-viewer-runs.md).
//
//   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
//     node tools/agent/verify_viewer_run.mjs <report_id> <out_dir>
//
// @playwright/test is resolved from frontend/node_modules regardless of cwd.
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'frontend');
const require = createRequire(join(frontendDir, 'package.json'));
const { chromium } = require('@playwright/test');

const [reportId, outDir] = process.argv.slice(2);
if (!reportId || !outDir) {
  console.error('usage: node verify_viewer_run.mjs <report_id> <out_dir>');
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });

const BASE = 'http://localhost:3000';
const OWNER = { email: 'admin@example.com', password: 'Password123!' };
const VIEWER = { email: 'viewer@example.com', password: 'Password123!' };

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });

async function loginContext(creds) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' });
  await page.fill('#email', creds.email);
  await page.fill('#password', creds.password);
  await page.click('form button[type=submit]');
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 30000 });
  // Fresh orgs land on /onboarding — skip it so app pages are reachable.
  await page.waitForTimeout(1500);
  if (page.url().includes('onboarding')) {
    const skip = page.getByText('Skip onboarding');
    if (await skip.count()) {
      await skip.first().click();
      await page.waitForTimeout(2000);
    }
  }
  await page.close();
  return context;
}

async function shot(page, file) {
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${outDir}/${file}` });
  console.log(`captured ${file}`);
}

// ── Owner: Share modal toggle ────────────────────────────────────────────────
const ownerCtx = await loginContext(OWNER);
{
  const page = await ownerCtx.newPage();
  await page.goto(`${BASE}/reports/${reportId}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);

  // The ShareModal trigger lives in the ArtifactFrame header. After sharing
  // 'internal' its label is the selected visibility option.
  const trigger = page
    .getByRole('button')
    .filter({ hasText: /Share Dashboard|Organization|Anyone|Specific people/i })
    .first();
  await trigger.waitFor({ timeout: 20000 });
  await trigger.click();
  await page.getByText('Run on my behalf').waitFor({ timeout: 10000 });
  await shot(page, '1-share-modal-toggle-off.png');

  // Flip the toggle → shared_run_identity = 'creator'
  const toggle = page.locator('button[role=switch]').last();
  await toggle.click();
  await page.waitForTimeout(1200); // PUT + toast
  await shot(page, '2-share-modal-toggle-on.png');

  // Flip it back so the viewer run below executes as the VIEWER identity.
  await toggle.click();
  await page.waitForTimeout(1200);
  await page.close();
}

// ── Viewer: /r page before/after Run ─────────────────────────────────────────
const viewerCtx = await loginContext(VIEWER);
{
  const page = await viewerCtx.newPage();
  await page.goto(`${BASE}/r/${reportId}`, { waitUntil: 'networkidle' });
  const frame = page.frameLocator('iframe');
  await frame.getByText('creator snapshot').first().waitFor({ timeout: 30000 });
  await page.getByTitle("Re-run this dashboard's queries for you").waitFor({ timeout: 10000 });
  await shot(page, '3-viewer-before-run.png');

  await page.getByTitle("Re-run this dashboard's queries for you").click();
  await frame.getByText('2024-01').first().waitFor({ timeout: 60000 });
  await shot(page, '4-viewer-after-run.png');
}

// ── Owner still sees the untouched shared snapshot ───────────────────────────
{
  const page = await ownerCtx.newPage();
  await page.goto(`${BASE}/r/${reportId}`, { waitUntil: 'networkidle' });
  const frame = page.frameLocator('iframe');
  await frame.getByText('creator snapshot').first().waitFor({ timeout: 30000 });
  await shot(page, '5-owner-after-viewer-run.png');
}

await browser.close();
console.log('ALL CAPTURES DONE');
