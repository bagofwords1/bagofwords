// Screenshots for the console Auto-scope loop (docs/feedback-loops/console-auto-scope.md).
// Signs in through the real form as <email>, opens Monitoring → Diagnosis and,
// when a run row is listed, opens its trace modal.
//
//   node tools/agent/console_auto_scope_shots.mjs <email> <out-prefix>
//   -> <out-prefix>-diagnosis.png, <out-prefix>-trace.png (only when a row exists)
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const [email, prefix] = process.argv.slice(2);
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const password = process.env.BOW_PASSWORD || 'Password123!';
mkdirSync(dirname(prefix), { recursive: true });

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
const page = await context.newPage();

await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', email);
await page.fill('#password', password);
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.click('button[type=submit]'),
]);
await page.waitForTimeout(1500);

await page.goto(`${origin}/monitoring/diagnosis`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(3000);
await page.screenshot({ path: `${prefix}-diagnosis.png` });
const rows = await page.locator('[data-testid="run-row"]').count();
console.log(JSON.stringify({ email, rows }));

if (rows) {
  await page.locator('[data-testid="run-row"]').last().click();
  await page.waitForTimeout(3500);
  await page.screenshot({ path: `${prefix}-trace.png` });
  const hidden = await page.locator('[data-testid="trace-hidden-turns"]').count();
  console.log(JSON.stringify({ trace_hidden_note: hidden }));
}
await browser.close();
