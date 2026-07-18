// Drive a real RCA conversation over the Elastic data source and capture evidence.
// NOTE: @playwright/test resolves from the script's own directory (ESM), so run
// these from a copy inside frontend/ (e.g. frontend/.agent-tmp/) against a stack
// booted by tools/agent/boot_stack.sh. Credentials come from env vars only.
// Usage: node rca_convo.mjs "<prompt>" <tag> <max_wait_seconds>
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.OUT || '../media/es-cloud';
const BASE = 'http://localhost:3000';
const PROMPT = process.argv[2] || 'We saw elevated errors this morning (July 14). Do a root cause analysis on the logs: which service started failing, when exactly did it start, which host is implicated, and what is the likely root cause? Show the error spike over time and the top error messages.';
const TAG = process.argv[3] || 'rca';
const WAIT = parseInt(process.argv[4] || '420', 10);
mkdirSync(OUT, { recursive: true });

const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const ctx = await b.newContext({ viewport: { width: 1512, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };

await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'commit' });
await page.getByPlaceholder(/email/i).fill('admin@example.com');
await page.getByPlaceholder(/password/i).fill('Password123!');
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2500);
try { await page.getByText(/skip onboarding/i).click({ timeout: 3000 }); await page.waitForTimeout(1500); } catch {}

await page.goto(`${BASE}/`, { waitUntil: 'commit' });
await page.waitForTimeout(3500);
await shot(`20-${TAG}-home`);

// Type into the contenteditable prompt box.
const box = page.locator('[contenteditable=true]').first();
await box.click();
await page.keyboard.type(PROMPT, { delay: 5 });
await page.waitForTimeout(500);
await shot(`21-${TAG}-typed`);

const sendBtn = page.locator('button.rounded-full').last();
await sendBtn.click({ timeout: 8000 }).catch(async () => { await box.click(); await page.keyboard.press('Enter'); });
await page.waitForTimeout(3000);
console.log('url after submit:', page.url());

// Poll until the agent finishes (send box re-enabled / "completed" markers) or timeout.
let lastLen = 0, stableFor = 0;
for (let i = 0; i < Math.ceil(WAIT / 10); i++) {
  await page.waitForTimeout(10000);
  const t = await page.locator('body').innerText().catch(() => '');
  process.stdout.write(`.${t.length}`);
  if (t.length === lastLen) { stableFor += 10; } else { stableFor = 0; lastLen = t.length; }
  if (i % 6 === 5) await shot(`22-${TAG}-progress-${i}`);
  // finished heuristics: page text stable for 60s after some growth
  if (stableFor >= 60 && t.length > 2000) break;
}
console.log('\nfinal url:', page.url());
await shot(`23-${TAG}-final`);
await page.screenshot({ path: `${OUT}/24-${TAG}-final-full.png`, fullPage: true });
const finalText = await page.locator('body').innerText().catch(() => '');
console.log('BODY_TAIL:\n', finalText.slice(-3500));
await ctx.close(); await b.close(); console.log('RCA_FLOW_DONE');
