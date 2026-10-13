// MOCK-UP, not a real run: reuse a live sub-agent launch for the real chrome
// (wave line, cards, status row), then rewrite the on-screen text so it reads
// as an infrastructure RCA with one system per sub-agent (Splunk, Elastic,
// ServiceNow, vROps) and the connector icons the app ships. For marketing /
// docs framing only; no data behind it.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';
const OUT = process.env.EVIDENCE_DIR || '../media/pr/sub-agents';
mkdirSync(OUT, { recursive: true });
const BASE = process.env.BOW_FRONTEND_URL || 'http://localhost:3000';
const EMAIL = process.env.BOW_ADMIN_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_ADMIN_PASSWORD || 'Password123!';
const REAL_PROMPT = 'Paid orders dropped about 15% since mid-September. Why? Test the likely causes separately and tell me which one it is, with evidence, and which ones you ruled out.';
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
await page.fill('#email', EMAIL); await page.fill('#password', PASSWORD); await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
const token = await page.evaluate(async (c) => (await (await fetch('/api/auth/jwt/login', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: `username=${encodeURIComponent(c.email)}&password=${encodeURIComponent(c.password)}` })).json()).access_token, { email: EMAIL, password: PASSWORD });
const orgId = await page.evaluate(async (t) => (await (await fetch('/api/organizations', { headers: { Authorization: `Bearer ${t}` } })).json())[0].id, token);
const api = (path, init = {}) => page.evaluate(async ({ t, org, path, init }) => { const r = await fetch(path, { ...init, headers: { Authorization: `Bearer ${t}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' } }); return r.json(); }, { t: token, org: orgId, path, init });
const ds = (await api('/api/data_sources')).find((d) => d.name === 'Checkout Ops');
// The planner occasionally answers inline instead of delegating; retry on a
// fresh report until a wave launches.
let launched = false;
for (let attempt = 1; attempt <= 3 && !launched; attempt++) {
  const report = await api('/api/reports', { method: 'POST', body: JSON.stringify({ title: 'Checkout latency alert', files: [], data_sources: [ds.id] }) });
  log(`attempt ${attempt}: report ${report.id}`);
  await page.goto(`${BASE}/reports/${report.id}`, { waitUntil: 'domcontentloaded' });
  const input = page.locator('div.mention-input-field[contenteditable="true"]').first();
  await input.waitFor({ state: 'visible', timeout: 120000 }); await page.waitForTimeout(2500);
  await input.fill(REAL_PROMPT); await input.press('Enter');
  launched = await page.locator('[data-testid^="delegate-card-"]').first().waitFor({ state: 'visible', timeout: 150000 }).then(() => true).catch(() => false);
  if (!launched) log('no wave this attempt');
}
if (!launched) { log('no wave after 3 attempts'); await browser.close(); process.exit(1); }
await page.locator('text=/Wave 1/').first().waitFor({ state: 'visible', timeout: 90000 }).catch(() => log('wave line not seen in 90s'));
await page.waitForTimeout(2000);
try { await page.locator('.widget-header h3.widget-title').first().click({ timeout: 5000 }); await page.waitForTimeout(600); } catch {}

// ---- rewrite the frame ----
await page.evaluate(({ realPrompt }) => {
  const SYSTEMS = [
    ['Splunk — deploy audit and ingress 5xx', '/data_sources_icons/splunk.png'],
    ['Elastic — application error logs', '/data_sources_icons/elasticsearch.svg'],
    ['ServiceNow — changes and incidents in the window', '/data_sources_icons/servicenow.png'],
    ['vROps — host, VM and datastore saturation', '/data_sources_icons/aria_operations.png'],
  ];
  const walk = (root, fn) => { const it = document.createTreeWalker(root, NodeFilter.SHOW_TEXT); let n; const nodes = []; while ((n = it.nextNode())) nodes.push(n); nodes.forEach(fn); };
  // exact text swaps anywhere on the page
  const swaps = new Map([
    [realPrompt, 'P1 alert: checkout latency on the payments cluster has been above 2 s since 09:14 UTC. Find the root cause. Check logs, metrics, changes and incidents separately and tell me which one it is, with evidence, and what you ruled out.'],
    ['Checkout Ops', 'Payments Infra'],
  ]);
  walk(document.body, (t) => { const v = t.nodeValue.trim(); if (swaps.has(v)) t.nodeValue = t.nodeValue.replace(v, swaps.get(v)); });
  // widget title
  const wt = document.querySelector('.widget-header h3.widget-title'); if (wt) wt.textContent = 'p99 checkout latency, payments cluster, 08:00–10:00 UTC';
  // the entity label on the Created Data row ("orders")
  walk(document.body, (t) => { if (t.nodeValue.trim() === 'orders') t.nodeValue = t.nodeValue.replace('orders', 'latency'); });
  // the assistant message that holds the cards, rewritten in document order:
  // paragraphs before the cards -> intro + confirmation; paragraphs after ->
  // one status line; the knowledge strip is dropped.
  const firstCard = document.querySelector('[data-testid^="delegate-card-"]');
  const cardsAll = Array.from(document.querySelectorAll('[data-testid^="delegate-card-"]'));
  const lastCard = cardsAll[cardsAll.length - 1];
  let msg = firstCard; while (msg && msg.querySelectorAll('p').length < 2) msg = msg.parentElement;
  if (msg) {
    const ps = Array.from(msg.querySelectorAll('p')).filter((p) => p.textContent.trim().length > 20 && !cardsAll.some((c) => c.contains(p)));
    const beforeCards = ps.filter((p) => p.compareDocumentPosition(firstCard) & Node.DOCUMENT_POSITION_FOLLOWING);
    const afterCards = ps.filter((p) => p.compareDocumentPosition(lastCard) & Node.DOCUMENT_POSITION_PRECEDING);
    if (beforeCards[0]) beforeCards[0].textContent = 'I\u2019ll confirm the alert window first with one query, then test the candidate causes in parallel, one system each.';
    if (beforeCards.length > 1) { beforeCards[beforeCards.length - 1].innerHTML = '<strong>Confirmed:</strong> p99 latency on /api/checkout rose from 310 ms to 2.4 s at 09:12 UTC and has stayed there; the error rate is flat, so this is slowness, not failures. Now I\u2019ll test the candidate causes separately, one system each.'; beforeCards.slice(1, -1).forEach((p) => p.remove()); }
    if (afterCards[0]) { afterCards[0].textContent = 'Four sub-agents are running in parallel: Splunk for the deploy audit and ingress logs, Elastic for application errors, ServiceNow for changes and incidents in the window, vROps for host and VM saturation. I\u2019ll report back when their records arrive.'; afterCards.slice(1).forEach((p) => p.remove()); }
    Array.from(msg.querySelectorAll('*')).filter((el) => el.children.length <= 3 && /Reviewing Knowledge/.test(el.textContent) && el.textContent.length < 120).forEach((el) => el.remove());
  }
  // cards: keep four (one per system), then title + icon
  const extra = Array.from(document.querySelectorAll('[data-testid^="delegate-card-"]')).slice(4);
  extra.forEach((c) => c.remove());
  if (extra.length) walk(document.body, (t) => { t.nodeValue = t.nodeValue.replace(/\b\d+ sub-agents\b/, '4 sub-agents').replace(/\b\d+ running\b/, '4 running').replace(/\b\d+ background tasks\b/, '4 background tasks'); });
  const cards = Array.from(document.querySelectorAll('[data-testid^="delegate-card-"]'));
  cards.forEach((card, i) => {
    const [title, icon] = SYSTEMS[i % SYSTEMS.length];
    let node = null;
    walk(card, (t) => { if (!node && /hypothesis|hypothes|check|mix|incident/i.test(t.nodeValue)) node = t; });
    if (!node) return;
    node.nodeValue = title;
    const el = node.parentElement;
    const img = document.createElement('img');
    img.src = icon; img.style.cssText = 'width:14px;height:14px;display:inline-block;vertical-align:-2px;margin-right:6px;border-radius:2px';
    el.insertBefore(img, node);
  });
  // report title in the header and the sidebar
  walk(document.body, (t) => { if (t.nodeValue.trim() === 'Checkout latency alert') t.nodeValue = t.nodeValue.replace('Checkout latency alert', 'Checkout latency alert — root cause'); });
}, { realPrompt: REAL_PROMPT });
await page.waitForTimeout(800);
await page.locator('text=P1 alert').first().scrollIntoViewIfNeeded();
await page.waitForTimeout(500);
await page.screenshot({ path: `${OUT}/mock-infra-rca-launch.png` });
log(`📸 ${OUT}/mock-infra-rca-launch.png — MOCK infra RCA framing`);
await browser.close();
