#!/usr/bin/env node
/**
 * Cohort C — real-browser users, run alongside the HTTP cohorts.
 *
 * Why bother when cohort A already drives the same API: the browser is where
 * "the app feels broken" actually happens. A completion whose SSE stream dies
 * shows up here as a stuck spinner or a "network error" toast, and the /agents
 * page has to stay responsive while all of that is going on. Cohort A can tell
 * us a stream dropped; only this tells us what the user saw.
 *
 * Two roles, mirroring the HTTP cohorts:
 *   chat   — open a report, send a prompt, wait for the run to finish
 *   browse — /agents and /instructions page loads + an instruction edit
 *
 * Scale note: each context is a real renderer process. On a 4-vCPU box only a
 * handful can run before the *client* becomes the bottleneck, so this cohort is
 * deliberately small and its own overhead is measured (see clientCpu below) so
 * the report can say whether its numbers are trustworthy.
 *
 * Usage: node ui_driver.js --users 6 --role mixed --duration 120 --out ui_L30.json
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const os = require('os');

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : def;
}

const N_USERS = parseInt(arg('users', '4'), 10);
const ROLE = arg('role', 'mixed');           // chat | browse | mixed
const DURATION = parseFloat(arg('duration', '120'));
const OUT = arg('out', 'ui_result.json');
const FRONT = arg('front', 'http://127.0.0.1:3000');
const STATE = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'sandbox_state.json')));

const results = [];
const errors = [];

function rec(o) { results.push({ t: Date.now() / 1000, ...o }); }

async function login(ctx, user) {
  const page = await ctx.newPage();
  const t0 = Date.now();
  await page.goto(`${FRONT}/users/sign-in`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.fill('#email', user.email);
  await page.fill('#password', STATE.password);
  await Promise.all([
    page.waitForURL(u => !u.toString().includes('sign-in'), { timeout: 60000 }),
    page.click('button[type=submit]'),
  ]);
  rec({ op: 'login', ms: Date.now() - t0, ok: true, user: user.email });
  return page;
}

async function browseLoop(page, user, stopAt) {
  while (Date.now() < stopAt) {
    for (const [op, url] of [['page_agents', '/agents'], ['page_instructions', '/instructions']]) {
      if (Date.now() >= stopAt) break;
      const t0 = Date.now();
      try {
        await page.goto(`${FRONT}${url}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
        // domcontentloaded fires before the data arrives; wait for the app to
        // actually settle so we time the page a user can use, not an empty shell.
        await page.waitForLoadState('networkidle', { timeout: 60000 }).catch(() => {});
        rec({ op, ms: Date.now() - t0, ok: true, user: user.email });
      } catch (e) {
        rec({ op, ms: Date.now() - t0, ok: false, user: user.email });
        errors.push(`${op}: ${String(e).slice(0, 140)}`);
      }
      await page.waitForTimeout(1000);
    }
  }
}

// Create the report through the API with the demo data source attached.
// PromptBoxV2 gates its send button on `canSubmit`, which requires a data
// source (or an uploaded file). Starting from `/` gives a report with neither,
// so the button stays disabled, the click does nothing, and the turn silently
// never runs — which is exactly what an earlier version of this driver scored
// as a fast success.
async function createReport(user) {
  const res = await fetch(`${STATE.base}/api/reports`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${user.token}`,
      'X-Organization-Id': user.org_id,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      title: `ui-${Date.now()}`,
      data_sources: STATE.data_source_id ? [STATE.data_source_id] : [],
    }),
  });
  if (!res.ok) throw new Error(`create report ${res.status}`);
  return (await res.json()).id;
}

async function chatLoop(page, user, stopAt) {
  while (Date.now() < stopAt) {
    const t0 = Date.now();
    let ok = false, sawError = false;
    try {
      const rid = await createReport(user);
      await page.goto(`${FRONT}/reports/${rid}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
      const input = page.locator('[contenteditable="true"]').first();
      await input.waitFor({ timeout: 30000 });
      await input.click();
      await input.type('List a few albums');
      const btn = page.locator('button[class*="rounded-full"]').last();
      // If the button never enables, the turn cannot run. Record the failure
      // rather than clicking a disabled control and calling it a success.
      await btn.waitFor({ state: 'visible', timeout: 20000 });
      const deadlineBtn = Date.now() + 20000;
      let enabled = false;
      while (Date.now() < deadlineBtn) {
        if (await btn.isEnabled()) { enabled = true; break; }
        await page.waitForTimeout(250);
      }
      if (!enabled) throw new Error('send button never enabled (canSubmit false)');
      await btn.click({ timeout: 20000 });
      const tFirst = Date.now() - t0;

      // Wait for the run to actually START before waiting for it to end.
      // Polling for "no stop button" immediately after send always succeeds on
      // the first tick — the run has not rendered yet — which reports a ~3s
      // "completed" turn for work that really takes tens of seconds.
      let started = false;
      const startDeadline = Date.now() + 60000;
      while (Date.now() < startDeadline) {
        const busy = (await page.locator('[data-testid="stop-button"]').count())
                   + (await page.getByText('Thinking', { exact: false }).count());
        if (busy > 0) { started = true; break; }
        await page.waitForTimeout(250);
      }
      const tStarted = Date.now() - t0;

      // Now it is meaningful to wait for the busy indicators to clear.
      const deadline = Date.now() + 300000;
      while (Date.now() < deadline) {
        const stopVisible = await page.locator('[data-testid="stop-button"]').count();
        const thinking = await page.getByText('Thinking', { exact: false }).count();
        if (!stopVisible && !thinking) { ok = started; break; }
        await page.waitForTimeout(1000);
      }
      // The exact user-visible failure this whole exercise is about.
      sawError = (await page.getByText(/network error/i).count()) > 0;
      rec({ op: 'chat_turn', ms: Date.now() - t0, first_ms: tFirst, started_ms: tStarted, started, ok, sawError, user: user.email });
    } catch (e) {
      rec({ op: 'chat_turn', ms: Date.now() - t0, ok: false, user: user.email });
      errors.push(`chat: ${String(e).slice(0, 140)}`);
    }
    await page.waitForTimeout(2000);
  }
}

(async () => {
  const users = STATE.users.filter(u => u.verified).slice(-N_USERS); // tail: avoid cohort A/B ids
  const browser = await chromium.launch({
    executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const cpu0 = process.cpuUsage();
  const stopAt = Date.now() + DURATION * 1000;

  const jobs = users.map(async (user, i) => {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    try {
      const page = await login(ctx, user);
      const role = ROLE === 'mixed' ? (i % 3 === 0 ? 'chat' : 'browse') : ROLE;
      if (role === 'chat') await chatLoop(page, user, stopAt);
      else await browseLoop(page, user, stopAt);
    } catch (e) {
      errors.push(`user ${user.email}: ${String(e).slice(0, 160)}`);
    } finally {
      await ctx.close().catch(() => {});
    }
  });

  await Promise.all(jobs);
  const cpu1 = process.cpuUsage(cpu0);
  await browser.close();

  const byOp = {};
  for (const r of results) {
    const d = byOp[r.op] || (byOp[r.op] = { n: 0, fail: 0, ms: [] });
    d.n++;
    if (r.ok) d.ms.push(r.ms); else d.fail++;
  }
  const pct = (a, p) => {
    if (!a.length) return null;
    const s = [...a].sort((x, y) => x - y);
    return s[Math.max(0, Math.min(s.length - 1, Math.round((p / 100) * (s.length - 1))))];
  };
  const summary = {
    users: users.length, role: ROLE, duration_s: DURATION,
    per_op: Object.fromEntries(Object.entries(byOp).map(([k, v]) =>
      [k, { n: v.n, fail: v.fail, p50: pct(v.ms, 50), p95: pct(v.ms, 95) }])),
    network_errors_seen: results.filter(r => r.sawError).length,
    // If the driver itself is pegged, its latencies describe the client.
    clientCpu: { user_s: cpu1.user / 1e6, system_s: cpu1.system / 1e6, cores: os.cpus().length },
    errors: errors.slice(0, 25),
  };
  fs.writeFileSync(OUT, JSON.stringify({ summary, raw: results }, null, 2));
  console.log(JSON.stringify(summary, null, 2));
})();
