import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
const BASE = 'http://localhost:3000'; const OUT = '../media/pr/feature-k8s-connector/e2e'; mkdirSync(OUT, { recursive: true });
const b = await chromium.launch(); const ctx = await b.newContext({ viewport: { width: 1512, height: 1000 } }); const page = await ctx.newPage(); page.setDefaultTimeout(30000);
const body = async () => (await page.locator('body').innerText().catch(() => ''));
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'commit' });
await page.locator('input[type=email]').first().fill('admin@example.com'); await page.locator('input[type=password]').first().fill('Password123!');
await Promise.all([page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}), page.locator('button[type=submit]').first().click()]);
await page.waitForTimeout(2500);
const PROMPTS = [
  ['deployments', 'List all deployments in the bow-test namespace with their ready and desired replica counts.', [/bow-runtime-app/i, /nats|mysql|postgres/i]],
  ['logs', 'Show me the last 20 log lines of the bow-runtime-app pod in the bow-test namespace.', [/bow-runtime-app/i, /GET|PUT|sources|HTTP|log/i]],
  ['restarts', 'Which pods in the cluster have restarted at least once, and what do the recent Warning events say?', [/restart/i, /event|warning/i]],
];
const results = [];
const ONLY = process.argv[2];
for (const [tag, prompt, expects] of PROMPTS.filter(([t]) => !ONLY || t === ONLY)) {
  await page.goto(`${BASE}/`, { waitUntil: 'commit' });
  const box = page.locator('[contenteditable=true]').first();
  await box.waitFor({ timeout: 30000 }); await page.waitForTimeout(1500);
  // pick the Kubernetes agent explicitly if an agent picker is offered
  try { const auto = page.getByRole('button', { name: /^auto$/i }).first(); if (await auto.count()) { await auto.click({ timeout: 2000 }); await page.waitForTimeout(500); await page.getByText(/kubernetes \(microk8s\)/i).first().click({ timeout: 2000 }); await page.waitForTimeout(400); } } catch { await page.keyboard.press('Escape').catch(() => {}); }
  await box.click(); await page.keyboard.type(prompt, { delay: 4 }); await page.waitForTimeout(500);
  const sendBtn = page.locator('button.rounded-full').last();
  await sendBtn.click({ timeout: 8000 }).catch(async () => { await box.click(); await page.keyboard.press('Enter'); });
  let last = '', stable = 0; const t0 = Date.now();
  while (Date.now() - t0 < 300000) {
    await page.waitForTimeout(5000);
    const t = await body();
    if (t === last) { stable += 5; if (stable >= 25 && t.length > prompt.length + 300) break; } else { stable = 0; last = t; }
  }
  const answer = last; const hits = expects.map((re) => re.test(answer));
  const errorish = /traceback|exception|failed to|error:/i.test(answer);
  const line = `C6-${tag} ${hits.every(Boolean) ? 'PASS' : 'FAIL'} | ${answer.length} chars in ${Math.round((Date.now() - t0) / 1000)}s | expectations ${JSON.stringify(hits)} | error-ish: ${errorish} | ${page.url()}`;
  console.log(line); results.push(line);
  await page.screenshot({ path: `${OUT}/c6-${tag}.png`, fullPage: true }); writeFileSync(`${OUT}/c6-${tag}.txt`, answer);
}
writeFileSync(`${OUT}/c6-results${ONLY ? '-' + ONLY : ''}.txt`, results.join('\n') + '\n');
await b.close();
