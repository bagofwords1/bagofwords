// Live capture of concurrent multi-tool streaming in the report conversation.
//
// Logs in through the real UI, opens (or creates via UI redirect) a report,
// submits a prompt through the chat box, and snapshots frames while the agent
// streams — the parallel tool cards are the evidence. Frames land in outDir
// as frame-<n>.png plus final.png.
//
// Usage (from frontend/ so @playwright/test resolves):
//   export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
//   node ../tools/agent/capture_parallel_flow.mjs \
//     --report <reportId> --out ../media/pr/concurrent-tools \
//     [--base http://localhost:3000] [--email admin@example.com] \
//     [--password Password123!] [--prompt "..."] [--frames 14] [--interval 1500]
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : dflt;
};

const base = opt('base', 'http://localhost:3000');
const reportId = opt('report', null);
const outDir = opt('out', './parallel-capture');
const email = opt('email', 'admin@example.com');
const password = opt('password', 'Password123!');
const promptText = opt(
  'prompt',
  'Inspect the orders table in every connected data source, then create a per-region summary for each source.',
);
const frames = Number(opt('frames', '14'));
const intervalMs = Number(opt('interval', '1500'));

if (!reportId) {
  console.error('usage: node capture_parallel_flow.mjs --report <reportId> --out <dir> [...]');
  process.exit(2);
}

mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();

// 1. Login through the real form
await page.goto(`${base}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', email);
await page.fill('#password', password);
await Promise.all([
  page.waitForURL((u) => !String(u).includes('sign-in'), { timeout: 30000 }),
  page.click('button[type="submit"]'),
]);
console.log('logged in');

// 2. Open the report
await page.goto(`${base}/reports/${reportId}`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(1500);

// 3. Submit the prompt through the chat box
const editor = page.locator('[contenteditable="true"]').first();
await editor.click();
await editor.pressSequentially(promptText, { delay: 5 });
await page.waitForTimeout(300);
await editor.press('Enter');
console.log('prompt submitted');

// 4. Frame capture while the agent streams
for (let i = 0; i < frames; i++) {
  await page.waitForTimeout(intervalMs);
  const path = `${outDir}/frame-${String(i).padStart(2, '0')}.png`;
  await page.screenshot({ path });
  console.log(`captured ${path}`);
}

// 5. Final settled state
await page.waitForTimeout(4000);
await page.screenshot({ path: `${outDir}/final.png`, fullPage: false });
console.log(`captured ${outDir}/final.png`);

await context.close();
await browser.close();
