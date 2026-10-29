// Upward wheel ownership for report follow mode, in the real report UI.
// Needs the dev server (reads the page's setup state); no model or customer data.
// BOW_TEST_EMAIL=... BOW_TEST_PASSWORD=... node tests/reports/wheel-ownership.mjs
import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';

const base = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3000';
const api = process.env.BOW_BASE_URL || 'http://127.0.0.1:8000';
const email = process.env.BOW_TEST_EMAIL;
const password = process.env.BOW_TEST_PASSWORD;
if (!email || !password) throw new Error('Provide throwaway BOW_TEST_EMAIL and BOW_TEST_PASSWORD');

const login = await fetch(`${api}/api/auth/jwt/login`, { method: 'POST', body: new URLSearchParams({ username: email, password }) });
assert(login.ok, 'demo login');
const { access_token } = await login.json();
const auth = { Authorization: `Bearer ${access_token}` };
const orgs = await (await fetch(`${api}/api/organizations`, { headers: auth })).json();
const reportResponse = await fetch(`${api}/api/reports`, { method: 'POST', headers: { ...auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' }, body: JSON.stringify({ title: 'Wheel ownership check', data_sources: [] }) });
assert(reportResponse.ok, 'create report');
const report = await reportResponse.json();

const browser = await chromium.launch();
const results = {};
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  await page.goto(`${base}/users/sign-in`);
  await page.locator('#email').fill(email);
  await page.locator('#password').fill(password);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(url => !url.pathname.includes('sign-in'), { timeout: 90000 });
  if (page.url().includes('/onboarding')) {
    await page.getByRole('button', { name: 'Skip onboarding' }).click();
    await page.waitForURL(url => !url.pathname.includes('/onboarding'));
  }
  await page.goto(`${base}/reports/${report.id}`);
  await page.locator('.mention-input-field').first().waitFor({ timeout: 90000 });
  await page.waitForFunction(() => {
    for (const el of document.querySelectorAll('*')) {
      let c = el.__vueParentComponent;
      while (c) {
        if (c.setupState?.forceScrollToBottom && c.setupState?.messages) { window.__report = c.setupState; return true; }
        c = c.parent;
      }
    }
    return false;
  });

  // A transcript tall enough to scroll, ending in three nested vertical
  // scrollers: a plain one, one already at its top, and one that blocks
  // scroll chaining (overscroll-behavior: contain).
  await page.evaluate(() => {
    const s = window.__report;
    s.stopWatchStream(); s.stopPollingInProgressCompletion();
    s.isSplitScreen = false;
    const body = Array.from({ length: 40 }, (_, i) => `Paragraph ${i + 1} of the answer.`).join('\n\n');
    s.messages = [
      { id: 'wheel-user', role: 'user', status: 'success', prompt: { content: 'Show the full breakdown.' } },
      { id: 'wheel-answer', role: 'system', status: 'success', completion_blocks: [{ id: 'wheel-block', block_index: 0, status: 'completed', completed_at: '2026-10-10T09:00:00Z', content: body }] },
    ];
  });
  await page.waitForTimeout(500);
  await page.evaluate(() => {
    const host = document.querySelector('.chat-messages').firstElementChild;
    const make = (id, extra = '') => {
      const box = document.createElement('div');
      box.id = id;
      box.setAttribute('style', `height:120px;overflow-y:auto;border:1px solid #ccc;margin:8px 0;${extra}`);
      box.innerHTML = `<div style="height:600px">${id}</div>`;
      host.append(box);
      return box;
    };
    make('nested-mid').scrollTop = 200;
    make('nested-top');
    make('nested-contain', 'overscroll-behavior-y:contain');
    window.__report.forceScrollToBottom();
  });
  await page.waitForTimeout(300);

  const state = () => page.evaluate(() => {
    const c = document.querySelector('.chat-messages');
    return { following: window.__report.isFollowing, gap: c.scrollHeight - c.clientHeight - c.scrollTop, top: c.scrollTop };
  });
  const wheelOver = async selector => {
    await page.evaluate(() => window.__report.forceScrollToBottom());
    await page.waitForTimeout(200);
    const before = await state();
    assert(before.following && before.gap <= 4, `${selector}: starts following at the bottom`);
    await page.locator(selector).hover();
    await page.mouse.wheel(0, -80);
    await page.waitForTimeout(250);
    return state();
  };

  // 1. Nested scroller with room above: it takes the gesture; follow stays on.
  const midBefore = await page.locator('#nested-mid').evaluate(el => el.scrollTop);
  results.nestedScroll = await wheelOver('#nested-mid');
  results.nestedScroll.nestedMoved = midBefore - await page.locator('#nested-mid').evaluate(el => el.scrollTop);
  assert(results.nestedScroll.nestedMoved > 0, 'nested scroller moved up');
  assert.equal(results.nestedScroll.following, true, 'nested scroll keeps following');

  // 2a. Nested scroller at its top: the gesture chains to the conversation.
  results.nestedBoundary = await wheelOver('#nested-top');
  assert.equal(results.nestedBoundary.following, false, 'gesture chained past a nested top releases following');

  // 2b. At its top but blocking chaining: the conversation never gets it.
  results.nestedContain = await wheelOver('#nested-contain');
  assert.equal(results.nestedContain.following, true, 'overscroll containment keeps following');

  // 3. Directly over the conversation: release at once.
  results.conversation = await wheelOver('.markdown-content');
  assert.equal(results.conversation.following, false, 'upward wheel over the conversation releases following');

  // 3b. Same gesture cancels a forced pin queued in the same frame, so
  // content growth afterwards leaves the reader where they are.
  results.cancelForced = await page.evaluate(async () => {
    const c = document.querySelector('.chat-messages');
    c.scrollTop = Math.max(0, c.scrollHeight - c.clientHeight - 300);
    await new Promise(r => requestAnimationFrame(r));
    const top = c.scrollTop;
    window.__report.forceScrollToBottom();
    document.querySelector('.markdown-content').dispatchEvent(new WheelEvent('wheel', { deltaY: -40, bubbles: true }));
    const grow = document.createElement('div'); grow.style.height = '400px'; c.firstElementChild.append(grow);
    await new Promise(r => setTimeout(r, 250));
    return { following: window.__report.isFollowing, topBefore: top, topAfter: c.scrollTop };
  });
  assert.equal(results.cancelForced.following, false, 'forced pin cancelled stays released');
  assert(Math.abs(results.cancelForced.topAfter - results.cancelForced.topBefore) <= 2, 'queued forced scroll did not move the reader');

  assert.equal(errors.length, 0, `no browser errors: ${errors.join('; ')}`);
  console.log(JSON.stringify({ reportId: report.id, results }, null, 2));
} finally { await browser.close(); }
