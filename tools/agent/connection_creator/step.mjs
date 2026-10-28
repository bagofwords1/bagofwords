// Interactive-ish helper: node step.mjs <email> <path> <out-prefix> [js-steps-file]
import { chromium } from '@playwright/test';
import { readFileSync } from 'node:fs';
const [email, path, prefix, stepsFile] = process.argv.slice(2);
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) console.log('HTTP', r.status(), r.request().method(), r.url().replace('http://localhost:3000', '')); });
await page.goto('http://localhost:3000/users/sign-in', { timeout: 240000 });
await page.fill('#email', email);
await page.fill('#password', 'Password123!');
await page.click('button[type=submit]');
await page.waitForURL(u => !u.toString().includes('sign-in'), { timeout: 60000 });
await page.goto('http://localhost:3000' + path, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(3000);
let i = 0;
const shot = async (name) => { await page.waitForTimeout(800); await page.screenshot({ path: `${prefix}-${String(++i).padStart(2, '0')}-${name}.png` }); console.log('shot', name); };
if (stepsFile) {
  const fn = new Function('page', 'shot', `return (async () => { ${readFileSync(stepsFile, 'utf8')} })()`);
  try { await fn(page, shot); } catch (e) { console.log('STEP ERROR', e.message.split('\n')[0]); await shot('error'); }
} else { await shot('page'); }
await browser.close();
