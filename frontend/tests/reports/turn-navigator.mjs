// Turn navigator (BOW-114): a rail of ticks beside the transcript, one per
// prompt, with a hover preview and click-to-jump. Checks LTR (en) and RTL (he)
// in the real report UI. Needs the dev server (reads the page's setup state);
// no model or customer data — the transcript is injected client-side.
//
//   BOW_TEST_EMAIL=... BOW_TEST_PASSWORD=... node tests/reports/turn-navigator.mjs
//   EVIDENCE_DIR=../media/pr/<slug> ... also writes screenshots + a flow video.
import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const base = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3000';
const api = process.env.BOW_BASE_URL || 'http://127.0.0.1:8000';
const email = process.env.BOW_TEST_EMAIL;
const password = process.env.BOW_TEST_PASSWORD;
const evidence = process.env.EVIDENCE_DIR ? path.resolve(process.env.EVIDENCE_DIR) : null;
if (!email || !password) throw new Error('Provide throwaway BOW_TEST_EMAIL and BOW_TEST_PASSWORD');
if (evidence) fs.mkdirSync(evidence, { recursive: true });

const login = await fetch(`${api}/api/auth/jwt/login`, { method: 'POST', body: new URLSearchParams({ username: email, password }) });
assert(login.ok, 'login');
const { access_token } = await login.json();
const auth = { Authorization: `Bearer ${access_token}` };
const orgs = await (await fetch(`${api}/api/organizations`, { headers: auth })).json();
const reportResponse = await fetch(`${api}/api/reports`, { method: 'POST', headers: { ...auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' }, body: JSON.stringify({ title: 'Q3 revenue review', data_sources: [] }) });
assert(reportResponse.ok, 'create report');
const report = await reportResponse.json();

const TURNS = {
  en: [
    ['What were total sales last quarter?', 'Total sales for **Q3 2026** were **$4.82M**, up 12% from Q2. The increase was driven mostly by the Enterprise segment, which grew 21% while SMB stayed flat.'],
    ['Break that down by region', 'By region: **North America** $2.31M (48%), **EMEA** $1.54M (32%), **APAC** $0.97M (20%). APAC had the fastest growth at 27% quarter over quarter.'],
    ['Which products drove the APAC growth?', 'Three products account for most of it: the Analytics add-on (+41%), Team seats (+18%) and the new Connectors bundle, which launched in August.'],
    ['Show me the monthly trend as a line chart', 'Here is the monthly trend. July was the softest month; September set a new record at $1.79M, mostly from renewals that landed at quarter end.'],
    ['Compare with the same quarter last year', 'Year over year, Q3 is up **34%**. EMEA improved the most (+52%), helped by the new Frankfurt region. North America grew 28%.'],
    ['Who are the top 5 customers by revenue?', 'Top 5 customers: Northwind ($312K), Contoso ($288K), Fabrikam ($241K), Tailspin ($199K) and Wide World Importers ($176K). Together they are 25% of revenue.'],
    ['Any churn risk among them?', 'Tailspin is the one to watch: seat usage fell 30% over the last 6 weeks and their renewal is due in December. The other four are stable or growing.'],
    ['Summarize this for the board in 3 bullets', '- Q3 revenue $4.82M, +12% QoQ and +34% YoY\n- Growth led by Enterprise and APAC\n- One renewal risk (Tailspin, December) with a plan in place'],
  ],
  he: [
    ['מה היו סך המכירות ברבעון האחרון?', 'סך המכירות ברבעון השלישי של 2026 היה **4.82 מיליון דולר**, עלייה של 12% לעומת הרבעון הקודם. הגידול הגיע בעיקר מלקוחות אנטרפרייז.'],
    ['פרק את זה לפי אזור', 'לפי אזור: **צפון אמריקה** 2.31 מיליון (48%), **אירופה** 1.54 מיליון (32%), **אסיה-פסיפיק** 0.97 מיליון (20%). אסיה-פסיפיק צמחה הכי מהר, 27%.'],
    ['אילו מוצרים הובילו את הצמיחה באסיה?', 'שלושה מוצרים אחראים לרוב הצמיחה: תוסף האנליטיקה (+41%), מושבי צוות (+18%) וחבילת המחברים החדשה שהושקה באוגוסט.'],
    ['הצג את המגמה החודשית בגרף קווי', 'הנה המגמה החודשית. יולי היה החודש החלש ביותר; ספטמבר שבר שיא עם 1.79 מיליון דולר, בעיקר מחידושים בסוף הרבעון.'],
    ['השווה לאותו רבעון בשנה שעברה', 'בהשוואה שנתית, הרבעון השלישי עלה ב-**34%**. אירופה השתפרה הכי הרבה (+52%) בזכות האזור החדש בפרנקפורט.'],
    ['מי חמשת הלקוחות המובילים לפי הכנסות?', 'חמשת הלקוחות המובילים: Northwind, Contoso, Fabrikam, Tailspin ו-Wide World Importers. יחד הם 25% מההכנסות.'],
  ],
};

async function openReport(page, locale) {
  await page.addInitScript(l => { try { localStorage.setItem('bow.locale', l) } catch {} }, locale);
  await page.goto(`${base}/users/sign-in`, { timeout: 180000 });
  await page.locator('#email').fill(email);
  await page.locator('#password').fill(password);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(url => !url.pathname.includes('sign-in'), { timeout: 90000 });
  if (page.url().includes('/onboarding')) {
    await page.locator('button', { hasText: /skip/i }).first().click().catch(() => {});
    await page.waitForURL(url => !url.pathname.includes('/onboarding')).catch(() => {});
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
}

async function inject(page, turns, { split = false } = {}) {
  await page.evaluate(({ turns, split }) => {
    const s = window.__report;
    s.stopWatchStream(); s.stopPollingInProgressCompletion();
    s.isSplitScreen = split;
    const filler = Array.from({ length: 6 }, (_, i) => `Supporting detail ${i + 1}: the numbers above reconcile with the finance ledger for the same period.`).join('\n\n');
    s.messages = turns.flatMap(([q, a], i) => [
      { id: `nav-user-${i}`, role: 'user', status: 'success', prompt: { content: q } },
      { id: `nav-answer-${i}`, role: 'system', status: 'success', completion_blocks: [{ id: `nav-block-${i}`, block_index: 0, status: 'completed', completed_at: '2026-11-05T09:00:00Z', content: `${a}\n\n${filler}` }] },
    ]);
  }, { turns, split });
  await page.waitForTimeout(600);
  await page.evaluate(() => window.__report.forceScrollToBottom());
  await page.waitForTimeout(600);
}

const geometry = page => page.evaluate(() => {
  const r = el => el.getBoundingClientRect();
  const c = document.querySelector('.chat-messages');
  const rail = document.querySelector('[data-testid="turn-navigator"] nav');
  const text = document.querySelector('[data-testid="transcript"]');
  const preview = document.querySelector('[data-testid="turn-navigator-preview"]');
  const ticks = [...document.querySelectorAll('[data-testid="turn-navigator"] button')];
  return {
    dir: document.documentElement.dir,
    container: { left: r(c).left, right: r(c).right, top: r(c).top },
    rail: rail && { left: r(rail).left, right: r(rail).right, top: r(rail).top },
    textBox: { left: r(text).left, right: r(text).right },
    preview: preview && { left: r(preview).left, right: r(preview).right, text: preview.innerText },
    ticks: ticks.length,
    active: ticks.findIndex(t => t.getAttribute('aria-current') === 'location'),
    focused: ticks.indexOf(document.activeElement),
  };
});

const promptTop = (page, i) => page.evaluate(i => {
  const c = document.querySelector('.chat-messages');
  const el = c.querySelector(`[data-message-id="nav-user-${i}"]`);
  return el.getBoundingClientRect().top - c.getBoundingClientRect().top;
}, i);

const shot = async (page, name) => { if (evidence) await page.screenshot({ path: path.join(evidence, name) }); };

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const results = {};
try {
  for (const locale of ['en', 'he']) {
    const turns = TURNS[locale];
    const context = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      deviceScaleFactor: evidence ? 2 : 1,
      ...(evidence ? { recordVideo: { dir: path.join(evidence, `video-${locale}`), size: { width: 1280, height: 800 } } } : {}),
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await openReport(page, locale);
    await inject(page, turns);

    // One tick per prompt; at the bottom the last turn is current.
    let g = await geometry(page);
    assert.equal(g.dir, locale === 'he' ? 'rtl' : 'ltr', `${locale}: document direction`);
    assert.equal(g.ticks, turns.length, `${locale}: one tick per prompt`);
    assert.equal(g.active, turns.length - 1, `${locale}: last turn active at bottom`);
    // Rail hugs the inline-start edge and stays out of the text column.
    if (g.dir === 'rtl') {
      assert(g.container.right - g.rail.right < 12, `${locale}: rail on the right edge`);
    } else {
      assert(g.rail.left - g.container.left < 12, `${locale}: rail on the left edge`);
    }
    await shot(page, `${locale}-rail.png`);

    // Hover a tick → preview with the prompt and the start of its answer,
    // opening toward the text (inline-end of the rail).
    const tick = page.locator('[data-testid="turn-navigator"] button').nth(2);
    await tick.hover();
    await page.locator('[data-testid="turn-navigator-preview"]').waitFor();
    g = await geometry(page);
    assert(g.preview.text.includes(turns[2][0]), `${locale}: preview shows the prompt`);
    assert(g.preview.text.includes(turns[2][1].replace(/\*\*/g, '').slice(0, 20)), `${locale}: preview shows the answer, markdown stripped`);
    if (g.dir === 'rtl') assert(g.preview.right <= g.rail.left + 1, `${locale}: preview opens to the left in RTL`);
    else assert(g.preview.left >= g.rail.right - 1, `${locale}: preview opens to the right in LTR`);
    await shot(page, `${locale}-hover.png`);

    // Click → that prompt lands at the top of the view and becomes current.
    await tick.click();
    await page.waitForTimeout(900);
    const top = await promptTop(page, 2);
    assert(top >= 0 && top < 60, `${locale}: jumped prompt is at the top (got ${top})`);
    await page.mouse.move(900, 400);
    await page.waitForTimeout(300);
    g = await geometry(page);
    assert.equal(g.active, 2, `${locale}: jumped turn is active`);
    assert.equal(g.preview, null, `${locale}: preview hides on leave`);
    await shot(page, `${locale}-after-jump.png`);

    // Scrolling by hand moves the active tick.
    await page.evaluate(() => { const c = document.querySelector('.chat-messages'); c.scrollTop = 0; });
    await page.waitForTimeout(300);
    g = await geometry(page);
    assert.equal(g.active, 0, `${locale}: first turn active at top`);

    // Keyboard: arrows move between ticks, Enter jumps.
    await page.locator('[data-testid="turn-navigator"] button').first().focus();
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('ArrowDown');
    g = await geometry(page);
    assert.equal(g.focused, 2, `${locale}: ArrowDown moves focus`);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(900);
    assert((await promptTop(page, 2)) < 60, `${locale}: Enter jumps`);

    // Split screen: narrow gutter → compact rail that stays clear of the text.
    await inject(page, turns, { split: true });
    g = await geometry(page);
    const railInner = g.dir === 'rtl' ? g.rail.left : g.rail.right;
    const textEdge = g.dir === 'rtl' ? g.textBox.right : g.textBox.left;
    const firstLine = await page.evaluate(() => {
      const t = document.querySelector('[data-testid="transcript"]');
      const cs = getComputedStyle(t);
      const r = t.getBoundingClientRect();
      return document.documentElement.dir === 'rtl' ? r.right - parseFloat(cs.paddingRight) : r.left + parseFloat(cs.paddingLeft);
    });
    const lineLen = await page.evaluate(() => [...document.querySelectorAll('[data-testid="turn-navigator"] .turn-nav-line')].reduce((m, el) => Math.max(m, el.getBoundingClientRect().width), 0));
    results[`${locale}_split`] = { railInner, textEdge, firstLine, lineLen };
    await page.locator('[data-testid="turn-navigator"] button').nth(1).hover();
    await page.waitForTimeout(200);
    await shot(page, `${locale}-split-hover.png`);

    assert.deepEqual(errors, [], `${locale}: no page errors`);
    results[locale] = 'ok';
    await context.close();
  }
} finally {
  await browser.close();
}
console.log(JSON.stringify(results, null, 2));
