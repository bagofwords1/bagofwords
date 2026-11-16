import fs from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url));
export const { chromium } = require('@playwright/test');
export const ORIGIN = 'http://localhost:3000';
// Screenshots, videos and result JSON land here.
export const SP = process.env.EVIDENCE_DIR || `${process.cwd()}/connection-queries-evidence`;
// Written by seed.py: agent and connection ids for the two warehouses.
export const LIVE_JSON = process.env.LIVE_JSON || `${SP}/live.json`;
export async function browser() { return chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {}); }
export async function loginContext(b, email, { viewport = { width: 1440, height: 900 }, video = false } = {}) {
  const ctx = await b.newContext({ viewport, deviceScaleFactor: 2, ...(video ? { recordVideo: { dir: `${SP}/video`, size: viewport } } : {}) });
  const page = await ctx.newPage();
  await page.goto(`${ORIGIN}/users/sign-in`);
  await page.fill('#email', email);
  await page.fill('#password', 'Password123!');
  await page.click('button[type=submit]');
  await page.waitForURL(u => !u.pathname.includes('sign-in'), { timeout: 90000 });
  if (page.url().includes('/onboarding')) {
    await page.getByText('Skip onboarding').click({ timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(1500);
  }
  return { ctx, page };
}
export const sleep = ms => new Promise(r => setTimeout(r, ms));

fs.mkdirSync(SP, { recursive: true });
