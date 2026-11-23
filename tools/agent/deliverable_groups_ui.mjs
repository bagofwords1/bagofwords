// Capture a LIVE training-mode run on /reports/:id whose chain mixes groupable
// calls with deliverables (create_instruction, edit_instruction,
// create_artifact). Pair with deliverable_groups_stub_llm.py (see its header):
// the stub holds the final answer, so the run is still live when we shoot.
//
//   cd frontend && node ../tools/agent/deliverable_groups_ui.mjs <out-dir> <label>
//
// Env: BOW_ORIGIN (default http://localhost:3000), BOW_EMAIL, BOW_PASSWORD,
//      BOW_LOCALE (e.g. he), DARK=1.
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';

const { chromium } = createRequire(import.meta.url)('playwright');
const [OUT = 'deliverable-groups', LABEL = 'after'] = process.argv.slice(2);
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const LOCALE = process.env.BOW_LOCALE || 'en';
const name = (s) => `${OUT}/${LABEL}-${LOCALE}${process.env.DARK ? '-dark' : ''}-${s}.png`;
mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log('[deliverable-groups]', ...a);

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1100, height: 2600 }, deviceScaleFactor: 2,
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

const prompt = LOCALE === 'he' ? 'צור לי דאשבורד על מכירות של אלבומים' : 'Build me a dashboard of album sales';
// A second tab holds the streaming request open for the whole run.
const runner = await context.newPage();
await runner.goto(`${BASE}/`);
const reportId = await runner.evaluate(async (content) => {
  const token = decodeURIComponent((document.cookie.match(/(?:^|; )auth\.token=([^;]*)/) || [])[1] || '');
  const auth = token.startsWith('Bearer') ? token : `Bearer ${token}`;
  const orgs = await (await fetch('/api/organizations', { headers: { Authorization: auth } })).json();
  const H = { Authorization: auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' };
  const ds = await (await fetch('/api/data_sources', { headers: H })).json();
  const music = ds.find((d) => /music/i.test(d.name)) || ds[0];
  const r = await (await fetch('/api/reports', { method: 'POST', headers: H, body: JSON.stringify({ title: 'Album sales', data_sources: [music.id], mode: 'training' }) })).json();
  window.__run = fetch(`/api/reports/${r.id}/completions`, {
    method: 'POST', headers: H,
    body: JSON.stringify({ prompt: { content, mode: 'training' }, stream: true }),
  }).then((res) => res.text());
  return r.id;
}, prompt);
log('report', reportId);

await page.goto(`${BASE}/reports/${reportId}`);
// Wait for the dashboard card: by then the whole chain before it is on screen.
const card = page.locator('.tool-execution-container').filter({ hasText: /Album sales/ }).first();
await card.waitFor({ state: 'visible', timeout: 180000 });
await page.waitForTimeout(2500);
// Close the dashboard panel the run opens, then shoot the whole turn.
const closePanel = page.locator('button:has(svg)').filter({ has: page.locator('[class*="x-mark"]') }).first();
await closePanel.click({ timeout: 3000 }).catch(() => {});
await page.waitForTimeout(800);
const turn = page.locator('[data-message-role="system"]').last();
const box = await turn.boundingBox();
const top = await page.locator('[data-message-role]').first().boundingBox();
const y = Math.max(0, (top?.y ?? box.y) - 16);
await page.screenshot({ path: name('live'), clip: { x: box.x - 16, y, width: box.width + 32, height: Math.min(2600 - y, box.y + box.height - y + 24) } });
log('shot', name('live'));
const groups = await page.locator('[data-testid="run-tool-group"]').allInnerTexts();
const rows = await turn.locator('.tool-call-label:visible').allInnerTexts();
const deliverables = await turn.locator('[data-testid="run-deliverables"]').count();
writeFileSync(name('live').replace(/\.png$/, '.json'), JSON.stringify({ reportId, groups, rows, deliverables }, null, 2));
log('groups', JSON.stringify(groups));
log('rows', JSON.stringify(rows), 'deliverables section:', deliverables);
await browser.close();
