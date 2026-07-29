// Cross-user UI evidence for shared-instruction authority.
//   cd frontend && node ../tools/agent/shoot_instruction_sharing.mjs
// Logs in as admin1 and admin2, captures the /agents (instructions) view for
// each, and drills into the shared instruction to show admin2's edit is gated.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const OUT = process.env.OUT_DIR || '/home/user/bagofwords/media/pr/shared-instruction-authority';
const execPath = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
mkdirSync(OUT, { recursive: true });

const SHARED_TEXT = 'SHARED RULE';

async function session(email, password) {
  const browser = await chromium.launch({ executablePath: execPath });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.fill('#email', email);
  await page.fill('#password', password);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
    page.click('button[type=submit]'),
  ]);
  await page.waitForTimeout(1500);
  if (page.url().includes('/onboarding')) {
    try { await page.getByText('Skip', { exact: false }).first().click({ timeout: 4000 }); } catch {}
    await page.waitForTimeout(1000);
  }
  return { browser, ctx, page };
}

async function shootAgents(label, email) {
  const { browser, page } = await session(email, 'Password123!');
  await page.goto(`${origin}/agents`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForTimeout(3500);
  await page.screenshot({ path: `${OUT}/${label}-agents.png`, fullPage: true });
  console.log(`${label}: /agents captured (url=${page.url()})`);

  // Try to open the shared instruction row and screenshot the detail/edit state.
  try {
    const row = page.getByText(SHARED_TEXT, { exact: false }).first();
    if (await row.count()) {
      await row.click({ timeout: 5000 });
      await page.waitForTimeout(2000);
      await page.screenshot({ path: `${OUT}/${label}-shared-detail.png`, fullPage: true });
      console.log(`${label}: shared instruction detail captured`);
    } else {
      console.log(`${label}: shared instruction row not found on page`);
    }
  } catch (e) {
    console.log(`${label}: detail capture skipped (${e.message})`);
  }
  await browser.close();
}

await shootAgents('admin1', 'admin@example.com');
await shootAgents('admin2', 'admin2ui@example.com');
console.log('done');
