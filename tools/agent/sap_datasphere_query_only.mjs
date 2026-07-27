// Focused: open an existing SAP Datasphere agent, start a report, ask a real
// analytical question, and verify Claude Haiku queried the semantic layer and
// returned server-side-aggregated data (revenue by country, US=3500).
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.OUT || '/home/user/bagofwords/media/sap-datasphere';
const BASE = 'http://localhost:3000';
mkdirSync(OUT, { recursive: true });

const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const ctx = await b.newContext({ viewport: { width: 1512, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(60000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };
const body = async () => (await page.locator('body').innerText());
const go = async (url) => { for (let i=0;i<3;i++){ try { await page.goto(url,{waitUntil:'domcontentloaded',timeout:60000}); return; } catch(e){ await page.waitForTimeout(3000);} } };

// login
await go(`${BASE}/users/sign-in`);
await page.getByPlaceholder(/email/i).fill('admin@example.com');
await page.getByPlaceholder(/password/i).fill('Password123!');
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2000);
try { await page.getByText(/skip onboarding/i).click({ timeout: 4000 }); await page.waitForTimeout(1200); } catch {}

// open the Agents page and pick a Datasphere agent
await go(`${BASE}/agents`);
await page.waitForTimeout(2500);
await page.getByText(/Datasphere Agent/i).first().click();
await page.waitForTimeout(2500);
await shot('11a-agent-page');

// start a New report scoped to that agent
await page.getByRole('button', { name: /new report/i }).first().click();
await page.waitForTimeout(3500);

// the prompt input is a contenteditable MentionInput (.mention-input-field)
async function findPrompt() {
  for (const c of [
    page.locator('.mention-input-field').first(),
    page.getByRole('textbox').first(),
    page.locator('[contenteditable="true"]').first(),
  ]) { if (await c.count()) return c; }
  return null;
}
let prompt = await findPrompt();
await shot('11-prompt-surface');
if (!prompt) { console.log('NO_PROMPT_FOUND'); await ctx.close(); await b.close(); process.exit(1); }

const QUESTION = 'What is the total revenue by country? Return a table sorted by revenue descending.';
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
console.log('QUERY_ONLY_DONE');
