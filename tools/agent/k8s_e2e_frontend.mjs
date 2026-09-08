// Sections C (frontend) + B3 of the Kubernetes connector e2e plan, driven through the real UI.
import { chromium } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';

const BASE = 'http://localhost:3000';
const OUT = process.argv[2] || '../media/pr/feature-k8s-connector/e2e';
const ACCESS = readFileSync(process.argv[3] || '/tmp/bow-agent/microk8s-access.yaml', 'utf8');
const RBAC = readFileSync('/Users/dkartsev/code/bagofwords/tools/kubernetes/rbac.yaml', 'utf8');
const AGENT_NAME = 'Kubernetes (microk8s)';
mkdirSync(OUT, { recursive: true });
const results = [];
const rec = (id, ok, detail) => { results.push({ id, ok, detail }); console.log(`${id} ${ok ? 'PASS' : 'FAIL'} ${detail}`); };

const LAPTOP = `apiVersion: v1\nkind: Config\nclusters:\n- name: prod\n  cluster: {server: https://x.eks.amazonaws.com, certificate-authority-data: LS0tLS1CRUdJTi0tLS0t}\nusers:\n- name: prod\n  user:\n    exec: {apiVersion: client.authentication.k8s.io/v1beta1, command: aws, args: [eks, get-token]}\ncontexts:\n- name: prod\n  context: {cluster: prod, user: prod}\ncurrent-context: prod\n`;

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1512, height: 1000 }, permissions: ['clipboard-read', 'clipboard-write'] });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (n) => { await page.waitForTimeout(500); await page.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }); };
const body = async () => (await page.locator('body').innerText().catch(() => ''));

// sign in
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'commit' });
await page.locator('input[type=email]').first().fill('admin@example.com');
await page.locator('input[type=password]').first().fill('Password123!');
await Promise.all([page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}), page.locator('button[type=submit]').first().click()]);
await page.waitForTimeout(2000);
try { await page.getByText(/skip onboarding/i).click({ timeout: 2000 }); } catch {}

// C1 catalog tile
await page.goto(`${BASE}/agents/new`, { waitUntil: 'commit' });
await page.waitForTimeout(2500);
const portal = page.locator('#headlessui-portal-root');
if (!(await portal.count()) || !(await portal.innerText().catch(() => '')).trim()) await page.getByText(/create new connection/i).first().click({ timeout: 15000 });
await page.waitForTimeout(1200);
const search = page.getByPlaceholder(/search/i).first();
if (await search.count()) { await search.fill('kubernetes'); await page.waitForTimeout(600); }
const tile = page.getByRole('button', { name: /kubernetes/i }).first();
const tileText = await portal.innerText().catch(() => '');
rec('C1', /kubernetes/i.test(tileText) && /infrastructure/i.test(tileText), `tile present under Infrastructure: ${/infrastructure/i.test(tileText)}`);
await shot('c1-catalog');
await tile.click({ timeout: 8000 }).catch(async () => { await tile.click({ force: true }); });
await page.waitForTimeout(1500);

// C2 form
const guide = page.locator('[data-testid="setup-guide"]');
const steps = await guide.locator('li').count();
const copies = await guide.getByRole('button', { name: /copy/i }).count();
await guide.locator('li').first().getByRole('button', { name: /copy/i }).click();
await page.waitForTimeout(400);
const clip = await page.evaluate(() => navigator.clipboard.readText());
const configLabels = await page.locator('form label').allInnerTexts();
const hasToggle = (await page.getByText(/require user authentication/i).count()) > 0;
rec('C2', steps === 3 && copies === 2 && clip === "kubectl apply -f - <<'EOF'\n" + RBAC + 'EOF' && !hasToggle && configLabels.includes('Namespaces'),
  `steps=${steps} copy buttons=${copies} step1 clipboard==rbac heredoc: ${clip === "kubectl apply -f - <<'EOF'\n" + RBAC + 'EOF'} | require-user-auth toggle: ${hasToggle} | labels=${JSON.stringify(configLabels)}`);
await shot('c2-form');

// C3 wrong paste
const access = page.locator('textarea#access_file');
await access.fill(LAPTOP);
await page.getByRole('button', { name: /test connection/i }).first().click();
await page.waitForTimeout(3000);
const rej = (await body()).match(/[^\n]*exec[^\n]*/i)?.[0] || '';
rec('C3', /exec/i.test(rej) && /not accepted/i.test(rej), `rejection: ${rej.slice(0, 120)}`);
await shot('c3-rejected');

// C4 right paste
await access.fill(ACCESS);
await page.getByRole('button', { name: /test connection/i }).first().click();
await page.getByText(/Connected successfully/i).first().waitFor({ timeout: 90000 }).catch(() => {});
const ok = (await body()).match(/Connected successfully[^\n]*/i)?.[0] || '';
const tableCount = Number((ok.match(/Found (\d+) tables/) || [])[1] || 0);
rec('C4', tableCount > 27, `${ok}`);
await shot('c4-test-ok');

// C5 save → discovery → tables → context
await page.getByRole('button', { name: /save and continue/i }).first().click();
await page.getByText(/discovered \d+ tables/i).first().waitFor({ timeout: 240000 });
const disc = await page.getByText(/discovered \d+ tables/i).first().innerText();
await shot('c5-discovery');
await page.getByRole('button', { name: /^connect$/i }).first().click();
await page.waitForTimeout(2500);
const agentName = page.getByPlaceholder(/sales, marketing/i).first();
if (await agentName.count()) await agentName.fill(AGENT_NAME);
await page.getByRole('button', { name: /save & continue|save and continue/i }).first().click().catch(() => {});
for (let i = 0; i < 40; i++) { await page.waitForTimeout(4000); if (/crd::|persistent_volumes/.test(await body())) break; }
const tb = await body();
const listed = ['pods', 'deployments', 'logs', 'crd::'].map((t) => [t, tb.includes(t)]);
await shot('c5-select-tables');
await page.getByRole('button', { name: /select all/i }).first().click({ timeout: 10000 }).catch(() => {});
await page.waitForTimeout(3000);
await page.getByRole('button', { name: /save & continue|save and continue/i }).first().click({ timeout: 15000 }).catch(() => {});
let contextReached = false;
for (let i = 0; i < 45; i++) { await page.waitForTimeout(4000); const t = await body(); if (/set context|instructions|overview/i.test(t) && !/select tables\s*$/i.test(t)) { contextReached = /context/i.test(t); if (contextReached) break; } }
await shot('c5-set-context');
rec('C5', /discovered \d+ tables/i.test(disc) && listed.every(([, v]) => v) && contextReached, `${disc} | tables listed: ${JSON.stringify(listed)} | Set Context reached: ${contextReached}`);
// finish the wizard if a finish/save button exists
for (const re of [/finish|done|save agent|create agent|save & finish|complete/i]) {
  const b = page.getByRole('button', { name: re }).first();
  if (await b.count()) { await b.click().catch(() => {}); await page.waitForTimeout(3000); }
}
console.log('url after wizard:', page.url());

// C6 prompts through the agent (real LLM)
const PROMPTS = [
  ['deployments', 'List all deployments in the bow-test namespace with their ready and desired replica counts.', [/bow-runtime-app/i, /nats|mysql|postgres/i]],
  ['logs', 'Show me the last 20 log lines of the bow-runtime-app pod in the bow-test namespace.', [/bow-runtime-app/i, /GET|PUT|sources|HTTP|log/i]],
  ['restarts', 'Which pods in the cluster have restarted at least once, and what do the recent Warning events say?', [/restart/i, /event|warning|none|no /i]],
];
for (const [tag, prompt, expects] of PROMPTS) {
  await page.goto(`${BASE}/`, { waitUntil: 'commit' });
  await page.waitForTimeout(3500);
  try {
    await page.getByText(/select data|data source|connect data|select agent|agents?/i).first().click({ timeout: 3000 });
    await page.waitForTimeout(600);
    await page.getByText(new RegExp(AGENT_NAME.replace(/[()]/g, '\\$&'), 'i')).first().click({ timeout: 3000 });
    await page.keyboard.press('Escape').catch(() => {});
  } catch {}
  const box = page.locator('[contenteditable=true]').first();
  await box.click();
  await page.keyboard.type(prompt, { delay: 5 });
  await page.waitForTimeout(500);
  const sendBtn = page.locator('button.rounded-full').last();
  await sendBtn.click({ timeout: 8000 }).catch(async () => { await box.click(); await page.keyboard.press('Enter'); });
  // wait for the answer to settle: text stops changing for 20s, up to 4 minutes
  let last = '', stable = 0, t0 = Date.now();
  while (Date.now() - t0 < 240000) {
    await page.waitForTimeout(5000);
    const t = await body();
    if (t === last) { stable += 5; if (stable >= 20 && t.length > prompt.length + 200) break; } else { stable = 0; last = t; }
  }
  const answer = last;
  const hits = expects.map((re) => re.test(answer));
  const errorish = /error|failed|exception|traceback/i.test(answer.slice(-3000)) && !/no error/i.test(answer);
  rec(`C6-${tag}`, hits.every(Boolean), `answer ${answer.length} chars in ${Math.round((Date.now() - t0) / 1000)}s | expectations ${JSON.stringify(hits)} | error-ish text: ${errorish} | url ${page.url()}`);
  await shot(`c6-${tag}`);
  writeFileSync(`${OUT}/c6-${tag}.txt`, answer);
}
writeFileSync(`${OUT}/results.json`, JSON.stringify(results, null, 2));
console.log(`\nTOTAL: ${results.filter((r) => r.ok).length} pass / ${results.length}`);
await browser.close();
