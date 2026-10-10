// Real report UI with a deterministic stream; no model or customer data.
// BOW_TEST_EMAIL=... BOW_TEST_PASSWORD=... node tests/reports/reading-stream-evidence.mjs before|after
import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync, renameSync } from 'node:fs';
import assert from 'node:assert/strict';

const phase = process.argv[2] || 'after';
const base = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3000';
const api = process.env.BOW_BASE_URL || 'http://127.0.0.1:8000';
const out = process.env.SHOT_DIR || '../media/pr/report-reading-stream';
const email = process.env.BOW_TEST_EMAIL;
const password = process.env.BOW_TEST_PASSWORD;
if (!email || !password) throw new Error('Provide throwaway BOW_TEST_EMAIL and BOW_TEST_PASSWORD');
mkdirSync(out, { recursive: true });
const login = await fetch(`${api}/api/auth/jwt/login`, { method: 'POST', body: new URLSearchParams({ username: email, password }) });
assert(login.ok, 'demo login');
const { access_token } = await login.json();
const auth = { Authorization: `Bearer ${access_token}` };
const orgs = await (await fetch(`${api}/api/organizations`, { headers: auth })).json();
const reportResponse = await fetch(`${api}/api/reports`, { method: 'POST', headers: { ...auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' }, body: JSON.stringify({ title: 'Quarterly revenue review', data_sources: [] }) });
assert(reportResponse.ok, 'create demo report');
const report = await reportResponse.json();
const browser = await chromium.launch();
const results = [];
const prose = '# Quarterly revenue review\n\nRevenue increased **18%** this quarter. Growth came from repeat customers and a stronger enterprise pipeline.\n\n## What changed\n\n- Repeat purchases contributed most of the growth.\n- Enterprise accounts grew faster than smaller accounts.\n- Acquisition costs remained stable.\n\n| Segment | Revenue | Change |\n|---|---:|---:|\n| Enterprise | $184,000 | +24% |\n| Small business | $92,000 | +8% |\n\n### Recommended next step\n\nCompare retention by cohort before increasing acquisition spend. Keep the regional mix visible alongside the headline result.\n';
try {
  for (const locale of ['en', 'he']) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, recordVideo: locale === 'en' ? { dir: `${out}/video`, size: { width: 1440, height: 960 } } : undefined });
    await context.addInitScript(l => { if (window === window.top) localStorage.setItem('bow.locale', l); }, locale);
    const videoStarted = Date.now();
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(`${base}/users/sign-in`);
    await page.locator('#email').fill(email);
    await page.locator('#password').fill(password);
    await page.locator('button[type="submit"]').click();
    await page.waitForURL(url => !url.pathname.includes('sign-in'), { timeout: 90000 });
    if (page.url().includes('/onboarding')) {
      await page.getByRole('button', { name: locale === 'he' ? /דלג|דילוג/ : 'Skip onboarding' }).click();
      await page.waitForURL(url => !url.pathname.includes('/onboarding'));
    }
    await page.goto(`${base}/reports/${report.id}`);
    await page.locator('.mention-input-field').first().waitFor({ timeout: 90000 });
    await page.waitForFunction(() => {
      for (const el of document.querySelectorAll('*')) {
        let c = el.__vueParentComponent;
        while (c) {
          if (c.setupState?.handleStreamingEvent && c.setupState?.messages) { window.__reportReading = c.setupState; return true; }
          c = c.parent;
        }
      }
      return false;
    });
    const text = locale === 'he' ? '# סקירת הכנסות רבעונית\n\nההכנסות עלו ב־**18%** ברבעון האחרון. לקוחות חוזרים תרמו לצמיחה ולשיפור בהכנסות.\n\n## מה השתנה\n\n- רכישות חוזרות הובילו את הצמיחה.\n- לקוחות גדולים צמחו מהר יותר.\n- עלויות רכישת לקוחות נותרו יציבות.\n\n### הצעד הבא\n\nכדאי להשוות את שיעורי השימור לפני הגדלת תקציב השיווק. יש לבחון את התמהיל האזורי לצד התוצאה המרכזית.\n' : prose;
    await page.evaluate(({ text, locale }) => {
      const s = window.__reportReading;
      s.stopWatchStream(); s.stopPollingInProgressCompletion(); s.isStreaming = true;
      s.isSplitScreen = false;
      s.messages = [
        { id: 'reading-user', role: 'user', status: 'success', prompt: { content: locale === 'he' ? 'סכם את הכנסות הרבעון והצע את הצעד הבא.' : 'Summarize quarterly revenue and recommend the next step.' } },
        { id: 'reading-answer', role: 'system', status: 'success', completion_blocks: [
          ...['read_query', 'run_query'].map((tool_name, i) => ({ id: `reading-tool-${i}`, block_index: i, status: 'completed', completed_at: '2026-10-10T09:00:00Z', tool_execution: { id: `reading-execution-${i}`, tool_name, status: 'success', duration_ms: 1200, arguments_json: { title: locale === 'he' ? 'בדיקת נתוני ההכנסות' : 'Checked quarterly revenue' } } })),
          { id: 'reading-block', block_index: 2, status: 'completed', completed_at: '2026-10-10T09:00:00Z', content: text }
        ] }
      ];
    }, { text, locale });
    await page.waitForTimeout(1300);
    await page.screenshot({ path: `${out}/${phase}-${locale}.png` });
    if (locale === 'en') {
      await page.evaluate(() => document.documentElement.classList.add('dark'));
      await page.waitForTimeout(500);
      await page.screenshot({ path: `${out}/${phase}-dark.png` });
      await page.evaluate(() => document.documentElement.classList.remove('dark'));
      await page.waitForTimeout(500);
    }
    const dimensions = await page.evaluate(() => {
      const root = document.querySelector('.chat-messages');
      const markdown = root.querySelector('.markdown-content');
      const heading = markdown.querySelector('h1');
      return { font: getComputedStyle(markdown).fontSize, lineHeight: getComputedStyle(markdown).lineHeight, heading: getComputedStyle(heading).fontSize, column: root.firstElementChild.getBoundingClientRect().width, dir: document.documentElement.dir };
    });
    if (locale === 'en') {
      await page.evaluate(() => {
        const s = window.__reportReading;
        s.messages[1].status = 'in_progress';
        s.messages[1].completion_blocks = [{ id: 'reading-block', block_index: 0, status: 'in_progress', content: '' }];
        s.forceScrollToBottom();
      });
      const streamStartSeconds = (Date.now() - videoStarted) / 1000;
      // Feed the production event handler with realistic word-sized deltas.
      for (const chunk of prose.match(/\S+\s*/g)) {
        await page.evaluate(async token => {
          const s = window.__reportReading;
          await s.handleStreamingEvent('block.delta.token', { block_id: 'reading-block', field: 'content', token }, 1);
          s.scheduleFollowScroll();
        }, chunk);
        await page.waitForTimeout(40);
      }
      await page.waitForTimeout(400);
      const completeText = await page.locator('.markdown-content').first().textContent();
      await page.evaluate(() => { const b = window.__reportReading.messages[1].completion_blocks[0]; b.status = 'completed'; b.completed_at = '2026-10-10T09:00:00Z'; });
      await page.waitForTimeout(100);
      assert.equal(await page.locator('.markdown-content').first().textContent(), completeText, 'finalization preserves answer text');
      await page.evaluate(() => { const b = window.__reportReading.messages[1].completion_blocks[0]; b.status = 'in_progress'; delete b.completed_at; });
      const animation = await page.evaluate(() => {
        const root = document.querySelector('.chat-messages');
        const el = root.querySelector('.markdown-content');
        return { duration: getComputedStyle(el).getPropertyValue('--stream-update-fade-duration').trim() || '900ms', bottomGap: root.scrollHeight - root.clientHeight - root.scrollTop };
      });
      // Growing content without another stream event must still follow.
      await page.evaluate(() => {
        const tail = document.createElement('div'); tail.id = 'reading-late'; tail.style.height = '360px';
        document.querySelector('.chat-messages').firstElementChild.append(tail);
      });
      await page.waitForTimeout(250);
      const lateGap = await page.locator('.chat-messages').evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop);
      await page.locator('.chat-messages').hover();
      await page.mouse.wheel(0, -250);
      await page.waitForTimeout(250);
      const detachedTop = await page.locator('.chat-messages').evaluate(el => el.scrollTop);
      await page.evaluate(() => { document.querySelector('#reading-late').style.height = '560px'; window.__reportReading.scheduleFollowScroll(); });
      await page.waitForTimeout(250);
      const preservedTop = await page.locator('.chat-messages').evaluate(el => el.scrollTop);
      if (phase === 'after') {
        assert(lateGap <= 4, `late layout follows: gap ${lateGap}`);
        assert(Math.abs(preservedTop - detachedTop) <= 2, 'reader remains detached');
        assert.equal(dimensions.font, '15px'); assert.equal(dimensions.column, 768);
        assert.equal(parseFloat(animation.duration) * (animation.duration.endsWith('ms') ? 1 : 1000), 180);
      }
      await page.evaluate(() => { document.querySelector('#reading-late').remove(); window.__reportReading.jumpToLatest(); });
      await page.waitForTimeout(250);
      // Narrow pane and mobile maintain readable text without horizontal overflow.
      await page.evaluate(() => { const s = window.__reportReading; s.isSplitScreen = true; s.leftPanelWidth = 520; });
      await page.waitForTimeout(500);
      await page.screenshot({ path: `${out}/${phase}-split.png` });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForTimeout(500);
      await page.screenshot({ path: `${out}/${phase}-mobile.png` });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      if (phase === 'after') assert(!overflow, 'mobile has no page overflow');
      results.push({ locale, dimensions, animation, streamStartSeconds, lateGap, detachedTop, preservedTop, overflow, errors });
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await page.evaluate(async () => { const s = window.__reportReading; await s.handleStreamingEvent('block.delta.token', { block_id: 'reading-block', field: 'content', token: ' Additional details remain readable.' }, 1); });
      await page.waitForTimeout(80);
      const reduced = await page.evaluate(() => {
        const el = document.querySelector('.text-node-stream-delta');
        return el ? { animation: getComputedStyle(el).animationName, opacity: getComputedStyle(el).opacity } : null;
      });
      if (phase === 'after') { assert(reduced, 'reduced-motion appended text is mounted'); assert.equal(reduced.animation, 'none'); assert.equal(reduced.opacity, '1'); }
      results.at(-1).reducedMotion = reduced;
      const compactFont = await page.evaluate(() => { const root = document.querySelector('.chat-messages'); root.classList.add('compact-messages'); const size = getComputedStyle(root.querySelector('.markdown-content')).fontSize; root.classList.remove('compact-messages'); return size; });
      if (phase === 'after') assert.equal(compactFont, '13px');
      results.at(-1).compactFont = compactFont;
    } else {
      if (phase === 'after') { assert.equal(dimensions.dir, 'rtl'); assert.equal(dimensions.font, '15px'); }
      results.push({ locale, dimensions, errors });
    }
    writeFileSync(`${out}/${phase}.json`, JSON.stringify(results, null, 2));
    console.log(JSON.stringify({ locale, errors }));
    const video = page.video();
    await context.close();
    if (video) renameSync(await video.path(), `${out}/${phase}.webm`);
    if (phase === 'after') assert.equal(errors.length, 0, 'no browser errors');
  }
  writeFileSync(`${out}/${phase}.json`, JSON.stringify(results, null, 2));
  console.log(JSON.stringify({ phase, reportId: report.id, results }, null, 2));
} finally { await browser.close(); }
