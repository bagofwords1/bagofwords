// Capture a FINISHED (folded) chat run that ends on result cards: a scheduled
// task and a pending wait. Pair with result_cards_stub_llm.py (see its header).
//
//   cd frontend && node ../tools/agent/result_cards_ui.mjs <out-dir> <label>
//
// Env: BOW_ORIGIN (default http://localhost:3000), BOW_EMAIL, BOW_PASSWORD,
//      BOW_LOCALE (e.g. he), DARK=1, REPORT_ID (shoot an existing report).
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';

const { chromium } = createRequire(import.meta.url)('playwright');
const [OUT = 'result-cards', LABEL = 'after'] = process.argv.slice(2);
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const LOCALE = process.env.BOW_LOCALE || 'en';
const name = (s) => `${OUT}/${LABEL}-${LOCALE}${process.env.DARK ? '-dark' : ''}-${s}.png`;
mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log('[result-cards]', ...a);

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1100, height: 1600 }, deviceScaleFactor: 2,
  colorScheme: process.env.DARK ? 'dark' : 'light',
});
await context.addInitScript((l) => localStorage.setItem('bow.locale', l), LOCALE);
const page = await context.newPage();
page.setDefaultNavigationTimeout(240000);
page.on('pageerror', (e) => log('pageerror', e.message));

await page.goto(`${BASE}/users/sign-in`);
await page.fill('#email', process.env.BOW_EMAIL || 'admin@example.com');
await page.fill('#password', process.env.BOW_PASSWORD || 'Password123!');
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });

let reportId = process.env.REPORT_ID;
if (!reportId) {
  const prompt = LOCALE === 'he'
    ? 'תזמן לי בדיקה שבועית של הכנסות לפי מדינה, ותבדוק שוב אחרי שהטעינה של הלילה מסתיימת'
    : 'Schedule a weekly revenue-by-country check, and re-check once tonight\'s load finishes';
  reportId = await page.evaluate(async (content) => {
    const token = decodeURIComponent((document.cookie.match(/(?:^|; )auth\.token=([^;]*)/) || [])[1] || '');
    const auth = token.startsWith('Bearer') ? token : `Bearer ${token}`;
    const orgs = await (await fetch('/api/organizations', { headers: { Authorization: auth } })).json();
    const H = { Authorization: auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' };
    const ds = await (await fetch('/api/data_sources', { headers: H })).json();
    const music = ds.find((d) => /music/i.test(d.name)) || ds[0];
    const r = await (await fetch('/api/reports', { method: 'POST', headers: H, body: JSON.stringify({ title: 'Weekly revenue', data_sources: [music.id] }) })).json();
    // Non-streaming: returns once the run has finished.
    await fetch(`/api/reports/${r.id}/completions`, { method: 'POST', headers: H, body: JSON.stringify({ prompt: { content } }) });
    return r.id;
  }, prompt);
}
log('report', reportId);

await page.goto(`${BASE}/reports/${reportId}`);
const turn = page.locator('[data-message-role="system"]').last();
await turn.waitFor({ state: 'visible', timeout: 120000 });
await page.locator('[data-testid="run-fold-toggle"]').first().waitFor({ state: 'visible', timeout: 60000 }).catch(() => log('no fold row'));
await page.waitForTimeout(2500);

const box = await turn.boundingBox();
const top = await page.locator('[data-message-role]').first().boundingBox();
const y = Math.max(0, (top?.y ?? box.y) - 16);
await page.screenshot({ path: name('settled'), clip: { x: box.x - 16, y, width: box.width + 32, height: Math.min(1600 - y, box.y + box.height - y + 24) } });
log('shot', name('settled'));

const labels = await turn.locator('.tool-call-label:visible').allInnerTexts();
const cancel = await turn.locator('button[aria-label]').filter({ hasNotText: /./ }).count();
const deliverables = await turn.locator('[data-testid="run-deliverables"]').count();
writeFileSync(name('settled').replace(/\.png$/, '.json'), JSON.stringify({ reportId, labels, deliverables }, null, 2));
log('labels', JSON.stringify(labels), 'deliverables section:', deliverables, 'icon buttons:', cancel);
await browser.close();
