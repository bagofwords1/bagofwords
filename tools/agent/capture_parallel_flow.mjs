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

// Cloud sandboxes pre-provision chromium at /opt/pw-browsers/chromium; use it
// when present so a @playwright/test version mismatch can't force a download.
import { existsSync } from 'node:fs';
const exe = process.env.PW_CHROMIUM_PATH
  || (existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
const browser = await chromium.launch(exe ? { executablePath: exe } : {});
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();

// 1. Login through the real form
await page.goto(`${base}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', email);
await page.fill('#password', password);
await page.click('button[type="submit"]');
// SPA client-side redirect: poll the URL rather than waiting for a load event.
for (let i = 0; i < 60; i++) {
  if (!page.url().includes('sign-in')) break;
  await page.waitForTimeout(1000);
}
if (page.url().includes('sign-in')) {
  await page.screenshot({ path: `${outDir}/debug-login.png` });
  throw new Error('login did not redirect — see debug-login.png');
}
console.log('logged in ->', page.url());

// 2. Open the report
await page.goto(`${base}/reports/${reportId}`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(1500);

// 3. Submit the prompt through the chat box (skip with --watch-only to
//    frame-capture a completion something else started on this report)
if (!args.includes('--watch-only')) {
  // Dev-server first compile of the report page can take a while; wait
  // for the chat editor explicitly and leave a debug shot if it never shows.
  const editor = page.locator('[contenteditable="true"]').first();
  await editor.waitFor({ state: 'visible', timeout: 120000 }).catch(async () => {
    await page.screenshot({ path: `${outDir}/debug-no-editor.png` });
    throw new Error('chat editor not found — see debug-no-editor.png');
  });
  await editor.click();
  await editor.pressSequentially(promptText, { delay: 5 });
  await page.waitForTimeout(300);
  await editor.press('Enter');
  console.log('prompt submitted');
} else {
  console.log('watch-only: capturing existing activity');
}

// 4. Frame capture while the agent streams. Scroll every scrollable pane to
//    its bottom first — the streaming tool cards append below the fold.
const scrollToBottom = () => page.evaluate(() => {
  document.querySelectorAll('div').forEach((d) => {
    if (d.scrollHeight > d.clientHeight + 80) d.scrollTop = d.scrollHeight;
  });
});
for (let i = 0; i < frames; i++) {
  await page.waitForTimeout(intervalMs);
  await scrollToBottom();
  await page.waitForTimeout(250);
  const path = `${outDir}/frame-${String(i).padStart(2, '0')}.png`;
  await page.screenshot({ path });
  console.log(`captured ${path}`);
}
await scrollToBottom();

// 5. Final settled state
await page.waitForTimeout(4000);
await page.screenshot({ path: `${outDir}/final.png`, fullPage: false });
console.log(`captured ${outDir}/final.png`);

await context.close();
await browser.close();
