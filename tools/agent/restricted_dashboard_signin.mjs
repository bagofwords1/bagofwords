// A person given "Can edit" on a restricted dashboard opens its link while
// signed out (docs/feedback-loops/2026-11-25-restricted-dashboard-signed-out-404.md).
//
//   cd frontend && node ../tools/agent/restricted_dashboard_signin.mjs <dashboardId> <outDir>
//
// Setup: tools/agent/seed_restricted_dashboard.py. Env: BOW_ORIGIN (default
// http://localhost:3000), CHROMIUM_EXECUTABLE (optional), EDITOR_EMAIL/EDITOR_PASSWORD (the "Can edit" member),
// OUTSIDER_EMAIL/OUTSIDER_PASSWORD (a member with no grant).
//
// Checks, each printed PASS/FAIL:
//   anon      signed out -> the sign-in gate on /d/{id}, not /not_found
//   signin    Sign in as the editor -> back on /d/{id}, the dashboard renders
//   outsider  signed in, no grant -> the request-access page (unchanged)
import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require(require.resolve('@playwright/test', { paths: [process.cwd()] }))); }
catch { ({ chromium } = require('playwright')); }

const [id, outDir] = process.argv.slice(2);
if (!id || !outDir) { console.error('usage: restricted_dashboard_signin.mjs <dashboardId> <outDir>'); process.exit(2); }
const ORIGIN = process.env.BOW_ORIGIN || 'http://localhost:3000';
const link = `${ORIGIN}/d/${id}`;
mkdirSync(outDir, { recursive: true });

const results = [];
const check = (name, ok, detail) => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'} ${name}: ${detail}`); };

async function signIn(page, email, password) {
    await page.waitForURL(/\/users\/sign-in/);
    await page.locator('input[type=email]').fill(email);
    await page.locator('input[type=password]').fill(password);
    await page.keyboard.press('Enter');
    await page.waitForURL(u => !u.pathname.startsWith('/users/sign-in'), { timeout: 30000 });
}

// CHROMIUM_EXECUTABLE: a cloud sandbox whose pinned Playwright lacks its own
// browser build points this at /opt/pw-browsers/chromium.
const browser = await chromium.launch(process.env.CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {});
const viewport = { width: 1280, height: 800 };
try {
    // anon + signin: one context, as the person clicking the shared link.
    const ctx = await browser.newContext({ viewport });
    const page = await ctx.newPage();
    await page.goto(link, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    const gate = await page.locator('[data-testid=app-sign-in-gate]').count();
    await page.screenshot({ path: `${outDir}/1-signed-out.png` });
    check('anon', gate === 1 && new URL(page.url()).pathname === `/d/${id}`,
        `url=${new URL(page.url()).pathname} gate=${gate}`);
    if (gate === 1) {
        await page.locator('[data-testid=app-sign-in-gate] a, [data-testid=app-sign-in-gate] button').first().click();
        await signIn(page, process.env.EDITOR_EMAIL, process.env.EDITOR_PASSWORD);
        await page.waitForLoadState('networkidle');
        await page.waitForTimeout(4000);
        const path = new URL(page.url()).pathname;
        const body = await page.frameLocator('iframe').first().locator('[data-testid=restricted-dashboard-body]').count()
            .catch(() => 0);
        await page.screenshot({ path: `${outDir}/2-after-sign-in.png` });
        check('signin', path === `/d/${id}` && body === 1, `url=${path} dashboard_rendered=${body === 1}`);
    } else {
        check('signin', false, 'no sign-in gate to click');
    }
    await ctx.close();

    // outsider: a signed-in member with no grant keeps the request page.
    const ctx2 = await browser.newContext({ viewport });
    const p2 = await ctx2.newPage();
    await p2.goto(`${ORIGIN}/users/sign-in`);
    await signIn(p2, process.env.OUTSIDER_EMAIL, process.env.OUTSIDER_PASSWORD);
    await p2.goto(link, { waitUntil: 'networkidle' });
    await p2.waitForTimeout(2000);
    const request = await p2.locator('[data-testid=dashboard-access-request]').count();
    await p2.screenshot({ path: `${outDir}/3-outsider.png` });
    check('outsider', request === 1, `url=${new URL(p2.url()).pathname} request_page=${request === 1}`);
    await ctx2.close();
} finally {
    await browser.close();
}
process.exit(results.every(Boolean) ? 0 : 1);
