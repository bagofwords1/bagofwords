// Feedback loop: a transient /api/users/whoami failure must not sign the user out.
// See 2026-11-23-session-logout-on-transient-failure.md.
//
//   BOW_EMAIL=... BOW_PASSWORD=... node docs/feedback-loops/2026-11-23-session-logout-on-transient-failure.verify.mjs
//
// Runs from any directory. Playwright is resolved from frontend/ (run
// `npx playwright install chromium` there once). BOW_CHROMIUM_EXECUTABLE
// overrides the browser binary, e.g. in a sandbox that ships a different build.
//
// Every trial has two phases, judged separately:
//   1. failure  - sign in, open a protected page, stub whoami to fail, trigger the
//                 check (tab refocus or reload), WAIT until the stub has actually
//                 served the expected failures, then assert the token and, for an
//                 already-open tab, that it is still on its protected page.
//   2. recovery - remove the stub, trigger the check again, assert the outcome.
// A trigger that never reaches whoami fails the trial instead of passing it.
// Exit code 1 if any trial differs from its expectation.
import { createRequire } from 'node:module';

const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('playwright');

const BASE = process.env.BOW_BASE_URL || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL;
const PASSWORD = process.env.BOW_PASSWORD;
if (!EMAIL || !PASSWORD) { console.error('set BOW_EMAIL and BOW_PASSWORD'); process.exit(2); }

const WHOAMI = '**/api/users/whoami';
const fail502 = { status: 502, body: 'Bad Gateway' };
const fail401 = { status: 401, body: '{"detail":"Unauthorized"}' };

// failures: how many whoami responses the stub fails (ofetch retries a GET once,
// so 1 is absorbed by the retry and >= 2 exhausts it). minServed: failures the
// stub must have served before phase 1 is judged.
const TRIALS = [
  { label: 'control: whoami healthy',             trigger: 'focus',  failure: null,    failures: 0,  minServed: 0, expect: { tokenAfterFailure: true,  stayOnPage: true,  signedInAfterRecovery: true } },
  { label: '502 once (absorbed by ofetch retry)', trigger: 'focus',  failure: fail502, failures: 1,  minServed: 1, expect: { tokenAfterFailure: true,  stayOnPage: true,  signedInAfterRecovery: true } },
  { label: '502 twice in a row',                  trigger: 'focus',  failure: fail502, failures: 2,  minServed: 2, expect: { tokenAfterFailure: true,  stayOnPage: true,  signedInAfterRecovery: true } },
  { label: 'network error twice in a row',        trigger: 'focus',  failure: 'abort', failures: 2,  minServed: 2, expect: { tokenAfterFailure: true,  stayOnPage: true,  signedInAfterRecovery: true } },
  { label: 'backend down (deploy/restart)',       trigger: 'focus',  failure: fail502, failures: 99, minServed: 2, expect: { tokenAfterFailure: true,  stayOnPage: true,  signedInAfterRecovery: true } },
  { label: 'page reload while backend down',      trigger: 'reload', failure: fail502, failures: 99, minServed: 2, expect: { tokenAfterFailure: true,  stayOnPage: null,  signedInAfterRecovery: true } },
  { label: '401 (session really revoked)',        trigger: 'focus',  failure: fail401, failures: 1,  minServed: 1, expect: { tokenAfterFailure: false, stayOnPage: null,  signedInAfterRecovery: false } },
];

async function signIn(page) {
  // A freshly started `nuxt dev` compiles pages on first request; allow for it.
  await page.goto(`${BASE}/users/sign-in`, { timeout: 120000 });
  await page.fill('input[type="email"]', EMAIL);
  await page.fill('input[type="password"]', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL(u => !u.pathname.startsWith('/users/sign-in'), { timeout: 30000 });
  await page.waitForTimeout(2000);
}

const hasToken = async ctx => !!(await ctx.cookies()).find(c => c.name === 'auth.token')?.value;
const pathOf = page => new URL(page.url()).pathname;
const onSignIn = page => pathOf(page).startsWith('/users/sign-in');

async function waitFor(cond, timeoutMs, label) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) { if (cond()) return; await new Promise(r => setTimeout(r, 100)); }
  throw new Error(`timed out waiting for ${label}`);
}

const refocus = page => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));

async function run(browser, t) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const problems = [];
  const calls = [];
  try {
    await signIn(page);
    const protectedPath = pathOf(page);

    // Phase 1: failure.
    let left = t.failures;
    let served = 0;
    let okCalls = 0;
    await page.route(WHOAMI, route => {
      if (t.failure && left > 0) {
        left--; served++; calls.push('FAIL');
        return t.failure === 'abort' ? route.abort('connectionreset') : route.fulfill(t.failure);
      }
      okCalls++; calls.push('ok');
      return route.continue();
    });

    if (t.trigger === 'focus') await refocus(page);
    else await page.goto(`${BASE}${protectedPath}`).catch(() => {});

    if (t.minServed > 0) await waitFor(() => served >= t.minServed, 10000, `${t.minServed} stubbed whoami failure(s), got ${served}`);
    else await waitFor(() => okCalls >= 1, 10000, 'the refocus to reach whoami');
    await page.waitForTimeout(1500); // let getSession's catch and the cookie watcher settle

    const tokenAfterFailure = await hasToken(ctx);
    if (tokenAfterFailure !== t.expect.tokenAfterFailure) problems.push(`token after failure=${tokenAfterFailure}, expected ${t.expect.tokenAfterFailure}`);
    const pageAfterFailure = pathOf(page);
    if (t.expect.stayOnPage && pageAfterFailure !== protectedPath) problems.push(`open tab moved to ${pageAfterFailure}, expected to stay on ${protectedPath}`);

    // Phase 2: recovery, with the backend healthy again.
    await page.unroute(WHOAMI);
    let recoveryCalls = 0;
    await page.route(WHOAMI, route => { recoveryCalls++; return route.continue(); });
    if (t.trigger === 'focus' && !onSignIn(page) && tokenAfterFailure) {
      await refocus(page);
      await waitFor(() => recoveryCalls >= 1, 10000, 'the recovery refocus to reach whoami');
      await page.waitForTimeout(1500);
    } else {
      await page.goto(`${BASE}${protectedPath}`);
      await page.waitForTimeout(3000);
    }
    const signedInAfterRecovery = !onSignIn(page) && await hasToken(ctx);
    if (signedInAfterRecovery !== t.expect.signedInAfterRecovery) problems.push(`signed in after recovery=${signedInAfterRecovery}, expected ${t.expect.signedInAfterRecovery} (at ${pathOf(page)})`);

    console.log(`${problems.length ? 'FAIL' : 'PASS'}  ${t.label.padEnd(38)} whoami=[${calls.join(',')}] tokenAfterFailure=${tokenAfterFailure} pageAfterFailure=${pageAfterFailure} afterRecovery=${pathOf(page)}${problems.length ? '\n      - ' + problems.join('\n      - ') : ''}`);
  } catch (e) {
    problems.push(e.message.split('\n')[0]);
    console.log(`FAIL  ${t.label.padEnd(38)} whoami=[${calls.join(',')}]\n      - ${problems.join('\n      - ')}`);
  } finally {
    await ctx.close();
  }
  return problems.length === 0;
}

const browser = await chromium.launch(process.env.BOW_CHROMIUM_EXECUTABLE ? { executablePath: process.env.BOW_CHROMIUM_EXECUTABLE } : {});
let ok = true;
for (const t of TRIALS) ok = (await run(browser, t)) && ok;
await browser.close();
console.log(ok ? '\nALL PASS' : '\nSOME TRIALS FAILED');
process.exit(ok ? 0 : 1);
