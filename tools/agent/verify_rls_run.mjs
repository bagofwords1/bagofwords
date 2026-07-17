// Per-user-credential UI verification on a user_required Postgres data source
// with row-level security (feedback loop:
// docs/feedback-loops/shared-artifact-viewer-runs.md, Loop C).
//
//   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
//     node tools/agent/verify_rls_run.mjs <report_id> <out_dir>
//
// Scenes (each wait is an assertion — a wrong state times out):
//   1. owner /r        → alice's rows (shared snapshot, ran as owner=alice)
//   2. viewer /r       → same alice rows (snapshot) before running
//   3. viewer Run      → bob's rows (viewer identity → bob's stored creds → RLS)
//   4. owner flips run_identity to 'creator' (API) → viewer Run → alice's rows
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'frontend');
const require = createRequire(join(frontendDir, 'package.json'));
const { chromium } = require('@playwright/test');

const [reportId, outDir] = process.argv.slice(2);
if (!reportId || !outDir) {
  console.error('usage: node verify_rls_run.mjs <report_id> <out_dir>');
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });

const BASE = 'http://localhost:3000';
const API = 'http://localhost:8000';
const OWNER = { email: 'admin@example.com', password: 'Password123!' };
const VIEWER = { email: 'viewer@example.com', password: 'Password123!' };

// Distinct row sets: what alice vs bob may see through the RLS policy.
const ALICE_REVENUE = '2140';
const BOB_REVENUE = '375';

async function setRunIdentity(identity) {
  const login = await fetch(`${API}/api/auth/jwt/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username: OWNER.email, password: OWNER.password }),
  });
  const { access_token } = await login.json();
  const orgs = await (await fetch(`${API}/api/organizations`, {
    headers: { Authorization: `Bearer ${access_token}` },
  })).json();
  const resp = await fetch(`${API}/api/reports/${reportId}/visibility/artifact`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${access_token}`,
      'X-Organization-Id': orgs[0].id,
    },
    body: JSON.stringify({ visibility: 'internal', run_identity: identity }),
  });
  if (!resp.ok) throw new Error(`set run_identity ${identity}: ${resp.status}`);
  console.log(`run_identity -> ${identity}`);
}

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });

async function loginContext(creds) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'networkidle' });
  await page.fill('#email', creds.email);
  await page.fill('#password', creds.password);
  await page.click('form button[type=submit]');
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 30000 });
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

await setRunIdentity('viewer');

// 1. Owner sees the shared snapshot they materialized as alice.
const ownerCtx = await loginContext(OWNER);
{
  const page = await ownerCtx.newPage();
  await page.goto(`${BASE}/r/${reportId}`, { waitUntil: 'networkidle' });
  const frame = page.frameLocator('iframe');
  await frame.getByText(ALICE_REVENUE).first().waitFor({ timeout: 30000 });
  await shot(page, 'rls-1-owner-alice-rows.png');
  await page.close();
}

// 2-3. Viewer: snapshot first, own-credential rows after Run.
const viewerCtx = await loginContext(VIEWER);
const viewerPage = await viewerCtx.newPage();
{
  await viewerPage.goto(`${BASE}/r/${reportId}`, { waitUntil: 'networkidle' });
  const frame = viewerPage.frameLocator('iframe');
  await frame.getByText(ALICE_REVENUE).first().waitFor({ timeout: 30000 });
  await shot(viewerPage, 'rls-2-viewer-before-run-sees-snapshot.png');

  await viewerPage.getByTitle("Re-run this dashboard's queries for you").click();
  await frame.getByText(BOB_REVENUE).first().waitFor({ timeout: 60000 });
  await shot(viewerPage, 'rls-3-viewer-own-credentials-bob-rows.png');
}

// 4. Creator mode: same viewer, same button — owner's credentials now execute.
await setRunIdentity('creator');
{
  const frame = viewerPage.frameLocator('iframe');
  await viewerPage.getByTitle("Re-run this dashboard's queries for you").click();
  await frame.getByText(ALICE_REVENUE).first().waitFor({ timeout: 60000 });
  await shot(viewerPage, 'rls-4-viewer-run-on-behalf-alice-rows.png');
}

await browser.close();
console.log('ALL RLS CAPTURES DONE');
