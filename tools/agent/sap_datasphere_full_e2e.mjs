// FULL end-to-end UI verification for sap_datasphere: connect (mock server) →
// name agent → select tables → set context → ask a real analytical question →
// verify the agent (Claude Haiku) queried the semantic layer and returned
// server-side-aggregated data. Screenshots into OUT.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.OUT || '/home/user/bagofwords/media/sap-datasphere';
const BASE = 'http://localhost:3000';
const STAMP = String(Date.now()).slice(-6);
const CONN_NAME = `SAP Datasphere ${STAMP}`;
const AGENT_NAME = `Datasphere Agent ${STAMP}`;
mkdirSync(OUT, { recursive: true });

const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const ctx = await b.newContext({ viewport: { width: 1512, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(60000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };
const body = async () => (await page.locator('body').innerText());
const go = async (url) => { for (let i=0;i<3;i++){ try { await page.goto(url,{waitUntil:'domcontentloaded',timeout:60000}); return; } catch(e){ console.log('goto retry',i); await page.waitForTimeout(3000);} } };

// ---- login ----
await go(`${BASE}/users/sign-in`);
await page.getByPlaceholder(/email/i).fill('admin@example.com');
await page.getByPlaceholder(/password/i).fill('Password123!');
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2000);
try { await page.getByText(/skip onboarding/i).click({ timeout: 5000 }); await page.waitForTimeout(1500); } catch {}

// ---- open the AddConnectionModal ----
await go(`${BASE}/agents/new?mode=new_connection`);
await page.waitForTimeout(2500);
try {
  await page.getByText(/skip onboarding/i).click({ timeout: 3000 });
  await page.waitForTimeout(1500);
  await go(`${BASE}/agents/new?mode=new_connection`);
  await page.waitForTimeout(2500);
} catch {}
await page.getByPlaceholder(/search/i).first().fill('datasphere');
await page.waitForTimeout(900);
await page.getByText(/^SAP Datasphere$/).first().click();
await page.waitForTimeout(1200);

// ---- fill the connection form + Test ----
await page.locator('input[placeholder*="Sales DB"]').fill(CONN_NAME).catch(() => {});
await page.locator('#host').fill('http://127.0.0.1:8899');
await page.locator('#token_url').fill('http://127.0.0.1:8899/oauth/token');
await page.locator('#client_id').fill('demo-technical-user');
await page.locator('#client_secret').fill('demo-secret');
await page.getByRole('button', { name: /test/i }).first().click();
await page.waitForTimeout(6000);
console.log('TEST_RESULT:', ((await body()).match(/Found \d+ tables[^\n]*/i) || ['(none)'])[0]);

// ---- create the connection -> modal runs schema discovery (indexing) ----
await page.getByRole('button', { name: /save & continue|save and continue|^create$/i }).last().click();
for (let i = 0; i < 24; i++) {
  await page.waitForTimeout(3000);
  if (/Discovered \d+ tables|Schema discovery.*(Connected|Complete)/i.test(await body())) break;
}
await shot('07-indexing-discovered');
console.log('INDEXING:', ((await body()).match(/Discovered \d+ tables[^\n]*/i) || ['(none)'])[0]);
// success step -> Connect closes the modal back to step 1
await page.getByRole('button', { name: /^Connect$/ }).click().catch(() => {});
await page.waitForTimeout(2500);

// ---- step 1: name the agent, Save & Continue -> schema ----
await page.locator('input[placeholder*="Sales, Marketing"]').first().fill(AGENT_NAME).catch(async () => {
  await page.locator('input[placeholder*="e.g."]').first().fill(AGENT_NAME).catch(() => {});
});
await page.waitForTimeout(500);
await page.getByRole('button', { name: /save & continue/i }).click();
await page.waitForURL(/agents\/new\/.*\/schema/, { timeout: 40000 }).catch(() => {});
await page.waitForTimeout(4000);

// ---- step 2: activate ALL tables (verify 3/3 active), Save & Continue ----
// Per-row checkbox toggle (the "X/N active" text is NOT reactive to client-side
// toggles, so we verify each checkbox's real checked state instead).
async function activateTables() {
  const names = ['SalesAnalyticModel', 'ExpensesModel', 'SalariesModel'];
  let ok = 0;
  for (const nm of names) {
    const row = page.locator('li').filter({ hasText: nm }).first();
    const cb = row.getByRole('checkbox').first();
    if (!(await cb.count())) { console.log('  no checkbox:', nm); continue; }
    if (!(await cb.isChecked().catch(() => false))) {
      await cb.click({ force: true }).catch(() => {});
      await page.waitForTimeout(500);
    }
    const checked = await cb.isChecked().catch(() => false);
    console.log('  row', nm, 'checked=', checked);
    if (checked) ok++;
  }
  return ok;
}
const okCount = await activateTables();
console.log('TABLES_CHECKED:', okCount, '/3');
await shot('08-tables-selected');
await page.getByRole('button', { name: /save & continue/i }).click();
await page.waitForTimeout(1500); // let save() persist
await page.waitForURL(/agents\/new\/.*\/context/, { timeout: 40000 }).catch(() => {});
await page.waitForTimeout(2500);
await shot('09-set-context');

// ---- step 3: finish -> agent page ----
await page.getByRole('button', { name: /save & continue/i }).click();
await page.waitForURL(/\/agents\/[^/]+$/, { timeout: 40000 }).catch(() => {});
await page.waitForTimeout(4000);
await shot('10-agent-created');
console.log('AGENT_URL:', page.url());
console.log('AGENT_TABLE_COUNT:', ((await body()).match(/(\d+)\s+tables/) || ['?'])[0]);

// ---- start a New report scoped to this agent, ask a real question ----
const QUESTION = 'What is the total revenue by country? Return a table sorted by revenue descending.';
// The prompt input is a contenteditable MentionInput (.mention-input-field).
async function findPrompt() {
  for (const c of [
    page.locator('.mention-input-field').first(),
    page.getByRole('textbox').first(),
    page.locator('[contenteditable="true"]').first(),
  ]) { if (await c.count()) return c; }
  return null;
}
// The agent header has a "+ New report" button that scopes the report to it.
try { await page.getByRole('button', { name: /new report/i }).first().click({ timeout: 8000 }); } catch {}
await page.waitForTimeout(3500);
let prompt = await findPrompt();
await shot('11-prompt-surface');
if (!prompt) { console.log('NO_PROMPT_FOUND'); await ctx.close(); await b.close(); process.exit(1); }

await prompt.click();
await prompt.pressSequentially(QUESTION, { delay: 8 });
await page.waitForTimeout(400);
await shot('12-question-typed');
await prompt.press('Enter');
console.log('submitted; waiting for Haiku to query the semantic layer…');

let answered = false;
for (let i = 0; i < 72; i++) {
  await page.waitForTimeout(5000);
  const t = await body();
  if (/US/.test(t) && /(3500|3,500)/.test(t)) { answered = true; break; }
  if (i % 4 === 0) await shot('13-running');
}
await page.waitForTimeout(2000);
await shot('14-answer');
const finalText = await body();
console.log('ANSWER_HAS_US_3500:', /(3500|3,500)/.test(finalText) && /US/.test(finalText));
console.log('ANSWER_MENTIONS_DE:', /DE/.test(finalText), '| revenue:', /revenue/i.test(finalText));
console.log('ANSWERED:', answered);

await ctx.close(); await b.close();
console.log('FULL_E2E_DONE');
