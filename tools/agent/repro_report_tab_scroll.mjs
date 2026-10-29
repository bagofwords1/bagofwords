// Repro: mobile Dashboard -> Chat tab switch loses the chat's scroll position
// and scroll tracking. See docs/feedback-loops/report-scroll-jump-artifact-reload.md.
//
//   cd frontend && node ../tools/agent/repro_report_tab_scroll.mjs <report_id>
//
// Expect after the fix: afterReturnToChat at the bottom (not top 0), and
// afterResize leaves a reader who scrolled up where they were.
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');
const [reportId] = process.argv.slice(2);
const origin = 'http://localhost:3000';
const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
const page = await ctx.newPage();
await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', 'admin@example.com'); await page.fill('#password', 'Password123!');
await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}), page.click('button[type=submit]')]);
await page.waitForTimeout(1500);
await page.goto(`${origin}/reports/${reportId}`);
await page.waitForSelector('.chat-messages', { timeout: 90000 });
await page.waitForTimeout(3000);
const st = () => page.evaluate(() => { const s = document.querySelector('.chat-messages'); return { top: Math.round(s.scrollTop), max: s.scrollHeight - s.clientHeight }; });
const out = { initial: await st() };
await page.getByRole('button', { name: /dashboard/i }).first().click(); await page.waitForTimeout(3000);
await page.getByRole('button', { name: /^chat$/i }).first().click(); await page.waitForTimeout(2000);
out.afterReturnToChat = await st();
// user scrolls to bottom, then reads upward
await page.evaluate(() => { const s = document.querySelector('.chat-messages'); s.scrollTop = s.scrollHeight; });
await page.waitForTimeout(300);
const b = await page.locator('.chat-messages').boundingBox();
await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
for (let i = 0; i < 10; i++) { await page.mouse.wheel(0, -150); await page.waitForTimeout(16); }
await page.waitForTimeout(300);
out.afterUserScrollUp = await st();
await page.setViewportSize({ width: 390, height: 780 }); await page.waitForTimeout(600);
out.afterResize = await st();
console.log(JSON.stringify(out, null, 1));
await browser.close();
