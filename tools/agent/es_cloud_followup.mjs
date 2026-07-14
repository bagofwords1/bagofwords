// Send a follow-up prompt in an existing report and capture the outcome.
// NOTE: @playwright/test resolves from the script's own directory (ESM), so run
// these from a copy inside frontend/ (e.g. frontend/.agent-tmp/) against a stack
// booted by tools/agent/boot_stack.sh. Credentials come from env vars only.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.env.OUT || '../media/es-cloud';
const BASE = 'http://localhost:3000';
const REPORT_ID = process.env.REPORT_ID;
const PROMPT = process.argv[2];
const TAG = process.argv[3] || 'followup';
const WAIT = parseInt(process.argv[4] || '420', 10);
if (!REPORT_ID || !PROMPT) { console.error('need REPORT_ID env + prompt arg'); process.exit(2); }
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
await page.waitForTimeout(2000);

await page.goto(`${BASE}/reports/${REPORT_ID}`, { waitUntil: 'commit' });
await page.waitForTimeout(4000);
const box = page.locator('[contenteditable=true]').first();
await box.click();
await page.keyboard.type(PROMPT, { delay: 4 });
await page.waitForTimeout(400);
await shot(`30-${TAG}-typed`);
const sendBtn = page.locator('button.rounded-full').last();
await sendBtn.click({ timeout: 8000 }).catch(async () => { await box.click(); await page.keyboard.press('Enter'); });
await page.waitForTimeout(3000);

let lastLen = 0, stableFor = 0;
for (let i = 0; i < Math.ceil(WAIT / 10); i++) {
  await page.waitForTimeout(10000);
  const t = await page.locator('body').innerText().catch(() => '');
  process.stdout.write(`.${t.length}`);
  if (t.length === lastLen) { stableFor += 10; } else { stableFor = 0; lastLen = t.length; }
  if (stableFor >= 60 && i > 3) break;
}
console.log('');
await shot(`31-${TAG}-final`);
await page.screenshot({ path: `${OUT}/32-${TAG}-final-full.png`, fullPage: true });
const finalText = await page.locator('body').innerText().catch(() => '');
console.log('BODY_TAIL:\n', finalText.slice(-3000));
await ctx.close(); await b.close(); console.log('FOLLOWUP_DONE');
