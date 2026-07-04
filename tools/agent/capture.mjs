// Screenshot / video capture helper for agent UI evidence.
//
// Run from frontend/ so @playwright/test resolves from its node_modules:
//
//   cd frontend
//   node ../tools/agent/capture.mjs http://localhost:3000/users/sign-in ../media/pr/sign-in.png
//   node ../tools/agent/capture.mjs http://localhost:3000/reports out.png --full
//   node ../tools/agent/capture.mjs http://localhost:3000/reports out.webm --video 8
//
// Auth'd pages: pass a bearer-token cookie/localStorage via a storage state
// file recorded once with --save-state (login flow is app-specific; see the
// ui-evidence skill for the scripted login snippet).
//
// In cloud sandboxes browsers live at /opt/pw-browsers:
//   export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
import { chromium } from '@playwright/test';
import { mkdirSync, renameSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';

const [url, out, ...rest] = process.argv.slice(2);
if (!url || !out) {
  console.error('usage: node capture.mjs <url> <out.png|out.webm> [--full] [--video <seconds>] [--state <state.json>]');
  process.exit(2);
}
const full = rest.includes('--full');
const videoIdx = rest.indexOf('--video');
const videoSecs = videoIdx >= 0 ? Number(rest[videoIdx + 1] || 8) : 0;
const stateIdx = rest.indexOf('--state');
const storageState = stateIdx >= 0 ? rest[stateIdx + 1] : undefined;

mkdirSync(dirname(out), { recursive: true });
const browser = await chromium.launch();
const videoDir = join(dirname(out), '.video-tmp');
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  ...(storageState ? { storageState } : {}),
  ...(videoSecs ? { recordVideo: { dir: videoDir, size: { width: 1440, height: 900 } } } : {}),
});
const page = await context.newPage();
await page.goto(url, { waitUntil: 'networkidle' }).catch(() => page.goto(url, { waitUntil: 'load' }));

if (videoSecs) {
  await page.waitForTimeout(videoSecs * 1000);
  await context.close();
  const file = readdirSync(videoDir).find((f) => f.endsWith('.webm'));
  renameSync(join(videoDir, file), out);
} else {
  await page.waitForTimeout(750); // let charts/fonts settle
  await page.screenshot({ path: out, fullPage: full });
  await context.close();
}
await browser.close();
console.log(`captured ${out}`);
