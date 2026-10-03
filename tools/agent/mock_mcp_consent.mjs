// Browser half of tools/agent/mock_mcp_oauth_client.py (--consent browser):
// play the user on BOW's real consent page — sign in if needed, pick the org,
// approve — and let the page redirect to the mock client's loopback callback.
//
// Usage (run from frontend/ so @playwright/test resolves):
//   node ../tools/agent/mock_mcp_consent.mjs <authorizeUrl> <email> <password> [orgName] [shotsDir]
// Env: BOW_FRONTEND_ORIGIN (default http://localhost:3000) — the authorize
// path is opened on this origin, as on a single-origin deployment.
// CHROMIUM_PATH — browser binary, when Playwright's pinned build is absent.
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';

// Resolve Playwright from the working directory (frontend/), not from this
// file's directory, which has no node_modules.
const { chromium } = createRequire(join(process.cwd(), 'package.json'))('@playwright/test');

const [authorizeUrl, email, password, orgName = '', shotsDir = ''] = process.argv.slice(2);
const origin = process.env.BOW_FRONTEND_ORIGIN || 'http://localhost:3000';
const target = new URL(authorizeUrl);
const consentUrl = `${origin}${target.pathname}${target.search}`;
if (shotsDir) mkdirSync(shotsDir, { recursive: true });

const shot = async (page, name) => {
  if (shotsDir) await page.screenshot({ path: join(shotsDir, name), fullPage: true });
};

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
try {
  const approve = page.getByRole('button', { name: /approve|allow|authorize/i });
  await page.goto(consentUrl);
  // /authorize renders first and then bounces signed-out users to sign-in.
  await page.locator('#email').or(approve).first().waitFor({ timeout: 30000 });
  if (await page.locator('#email').isVisible()) {
    await page.fill('#email', email);
    await page.fill('#password', password);
    await page.click('button[type="submit"]');
  }
  // A brand-new org is sent to onboarding first, dropping the consent URL;
  // skip it the way a user would and come back.
  const skip = page.getByText(/skip onboarding/i);
  await skip.or(approve).first().waitFor({ timeout: 30000 });
  if (await skip.isVisible()) {
    await skip.click();
    await page.waitForURL(url => !String(url).includes('/onboarding'), { timeout: 15000 }).catch(() => {});
    await page.goto(consentUrl);
  }
  await approve.waitFor({ timeout: 30000 });
  if (orgName) {
    const picker = page.locator('#oauth-org');
    if (await picker.count()) await picker.selectOption({ label: orgName });
  }
  await shot(page, 'consent.png');
  await Promise.all([
    page.waitForURL(url => new URL(String(url)).pathname === '/callback', { timeout: 30000 }),
    approve.click(),
  ]);
  console.log('consent approved; redirected to', new URL(page.url()).origin + new URL(page.url()).pathname);
} catch (err) {
  await shot(page, 'consent-error.png');
  console.error('consent failed:', err.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
