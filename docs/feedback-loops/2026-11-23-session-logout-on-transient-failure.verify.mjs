// Feedback loop: a transient /api/users/whoami failure must not sign the user out.
// See 2026-11-23-session-logout-on-transient-failure.md.
//
//   BOW_EMAIL=... BOW_PASSWORD=... node docs/feedback-loops/2026-11-23-session-logout-on-transient-failure.verify.mjs
//
// Each trial signs in, makes whoami fail N times (Playwright route stub), then
// either refocuses the tab or reloads the page, lets the backend "recover", and
// checks whether the user is still signed in. Exit code 1 if any trial differs
// from its expectation.
import { createRequire } from 'node:module';
// require() honours NODE_PATH and the cwd's node_modules, so the pre-installed
// sandbox Playwright or frontend/node_modules both work; ESM import would not.
const require = createRequire(`${process.cwd()}/`);
const { chromium } = require('playwright');

const BASE = process.env.BOW_BASE_URL || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL;
const PASSWORD = process.env.BOW_PASSWORD;
if (!EMAIL || !PASSWORD) { console.error('set BOW_EMAIL and BOW_PASSWORD'); process.exit(2); }

const fail502 = { status: 502, body: 'Bad Gateway' };
const TRIALS = [
  { label: 'control: whoami healthy',              trigger: 'focus',  failure: null,                      times: 0,  expectSignedIn: true },
  { label: '502 once (absorbed by ofetch retry)',  trigger: 'focus',  failure: fail502,                   times: 1,  expectSignedIn: true },
  { label: '502 twice in a row',                   trigger: 'focus',  failure: fail502,                   times: 2,  expectSignedIn: true },
  { label: 'network error twice in a row',         trigger: 'focus',  failure: 'abort',                   times: 2,  expectSignedIn: true },
  { label: 'backend down (deploy/restart)',        trigger: 'focus',  failure: fail502,                   times: 99, expectSignedIn: true },
  { label: 'page reload while backend down',       trigger: 'reload', failure: fail502,                   times: 99, expectSignedIn: true },
  { label: '401 (session really revoked)',         trigger: 'focus',  failure: { status: 401, body: '{"detail":"Unauthorized"}' }, times: 1, expectSignedIn: false },
];

async function signIn(page) {
  await page.goto(`${BASE}/users/sign-in`);
  await page.fill('input[type="email"]', EMAIL);
  await page.fill('input[type="password"]', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL(u => !u.pathname.startsWith('/users/sign-in'), { timeout: 30000 });
  await page.waitForTimeout(2000);
}

const hasToken = async ctx => !!(await ctx.cookies()).find(c => c.name === 'auth.token')?.value;

async function run(browser, t) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await signIn(page);
  let left = t.times;
  const calls = [];
  await page.route('**/api/users/whoami', route => {
    if (t.failure && left > 0) {
      left--; calls.push('FAIL');
      return t.failure === 'abort' ? route.abort('connectionreset') : route.fulfill(t.failure);
    }
    calls.push('ok');
    return route.continue();
  });

  if (t.trigger === 'focus') {
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  } else {
    await page.goto(`${BASE}/reports`).catch(() => {});
  }
  await page.waitForTimeout(2500);
  const tokenAfterFailure = await hasToken(ctx);

  // Backend recovers; the user keeps working.
  await page.unroute('**/api/users/whoami');
  await page.goto(`${BASE}/reports`);
  await page.waitForTimeout(3000);
  const path = new URL(page.url()).pathname;
  const signedIn = !path.startsWith('/users/sign-in') && await hasToken(ctx);
  await ctx.close();

  const pass = signedIn === t.expectSignedIn;
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${t.label.padEnd(38)} whoami=[${calls.join(',')}] tokenAfterFailure=${tokenAfterFailure} afterRecovery=${path} signedIn=${signedIn} (expected ${t.expectSignedIn})`);
  return pass;
}

const browser = await chromium.launch();
let ok = true;
for (const t of TRIALS) ok = (await run(browser, t)) && ok;
await browser.close();
console.log(ok ? '\nALL PASS' : '\nSOME TRIALS FAILED');
process.exit(ok ? 0 : 1);
