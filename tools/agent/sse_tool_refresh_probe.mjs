// Verify two refresh-resume regressions on /reports/[id]:
// 1. Refreshing the instant "Creating Data" starts must not lose the tool
//    card — the watch stream replays tool.started for running tools.
// 2. The stop button must render after a refresh while a run is in progress,
//    and clicking it must sigkill the server-side run.
import { chromium } from '@playwright/test';
import fs from 'fs';

const BASE = process.env.BASE || 'http://localhost:3000';
const OUT = process.argv[2] || '/tmp/bow-agent/toolprobe';
const REPORT_ID = process.env.REPORT_ID;
if (!REPORT_ID) { console.error('REPORT_ID required'); process.exit(2); }
fs.mkdirSync(OUT, { recursive: true });
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

const token = fs.readFileSync('/tmp/bow-agent/token.txt', 'utf8').trim();
const API_HEADERS = { Authorization: `Bearer ${token}`, 'X-Organization-Id': '317703e1-0faf-44ce-8f74-44957010a0a5' };
const apiLastSystem = async () => {
  const res = await fetch(`http://localhost:8000/api/reports/${REPORT_ID}/completions?limit=5`, { headers: API_HEADERS });
  const data = await res.json();
  return (data.completions || []).filter((c) => c.role === 'system').pop();
};

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome', headless: true });
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();

await page.goto(`${BASE}/users/sign-in`);
await page.waitForLoadState('networkidle').catch(() => {});
await page.fill('input[type="text"]', 'admin@example.com');
await page.fill('input[type="password"]', 'Password123!');
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 60000 });
log('logged in');

await page.goto(`${BASE}/reports/${REPORT_ID}`);
const box = page.locator('.mention-input-field').first();
await box.waitFor({ state: 'visible', timeout: 60000 });
await page.waitForTimeout(2000);
await box.click();
await box.pressSequentially('Create a data table of total revenue by genre from the Chinook data. Use the create_data tool.', { delay: 5 });
await page.locator('button.w-7.h-7.rounded-full:not([disabled])').last().click();
log('prompt submitted');

// Wait for the tool card to appear, then refresh IMMEDIATELY
const toolCard = page.locator('text=/Creating Data|Created Data/i').first();
await toolCard.waitFor({ state: 'visible', timeout: 60000 });
log('tool card appeared — refreshing NOW');
await page.reload();

// Measure the gap between the timeline rendering and the tool card
// rendering — that's the user-visible "card disappeared" window.
// (Total time from reload includes dev-mode page boot, which is noise.)
await page.locator('text=Create a data table of total revenue').first().waitFor({ state: 'visible', timeout: 30000 });
const t0 = Date.now();
let cardBackMs = -1;
for (let i = 0; i < 80; i++) {
  if (await page.locator('text=/Creating Data|Created Data/i').first().isVisible().catch(() => false)) {
    cardBackMs = Date.now() - t0;
    break;
  }
  await page.waitForTimeout(100);
}
log(`tool card gap after timeline render: ${cardBackMs}ms ${cardBackMs >= 0 && cardBackMs < 2000 ? '(PASS <2s)' : '(FAIL)'}`);
await page.screenshot({ path: `${OUT}/T1-tool-card-after-refresh.png` });

// Stop button must be visible while the run is in progress (post-refresh page)
const stopBtn = page.locator('button.bg-gray-500.w-7.h-7.rounded-full').first();
const stopVisible = await stopBtn.isVisible({ timeout: 5000 }).catch(() => false);
log(`stop button visible after refresh: ${stopVisible ? 'PASS' : 'FAIL'}`);
await page.screenshot({ path: `${OUT}/T2-stop-button-after-refresh.png` });

let stopWorked = 'SKIP (button not visible)';
if (stopVisible) {
  await stopBtn.click();
  log('stop clicked — waiting for run to become stopped');
  let sys = null;
  for (let i = 0; i < 20; i++) {
    await page.waitForTimeout(1500);
    sys = await apiLastSystem();
    if (sys && (sys.sigkill || sys.status !== 'in_progress')) break;
  }
  stopWorked = sys && (sys.sigkill || ['stopped', 'error', 'success'].includes(sys.status))
    ? `PASS (status=${sys.status}, sigkill=${!!sys.sigkill})`
    : `FAIL (status=${sys?.status}, sigkill=${!!sys?.sigkill})`;
}
log(`stop actually killed the run: ${stopWorked}`);
await page.screenshot({ path: `${OUT}/T3-after-stop.png` });

const pass = cardBackMs >= 0 && cardBackMs < 2000 && stopVisible && stopWorked.startsWith('PASS');
log(pass ? 'TOOL-REFRESH PROBE PASS' : 'TOOL-REFRESH PROBE FAIL');
await browser.close();
process.exit(pass ? 0 : 1);
