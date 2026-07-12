import { chromium } from '@playwright/test';
const [url, out, waitSel, extraMs] = process.argv.slice(2);
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const context = await browser.newContext({ viewport: { width: 1680, height: 950 }, storageState: 'state.json' });
const page = await context.newPage();
page.on('pageerror', e => console.log('PAGEERROR:', String(e).slice(0,200)));
await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 120000 });
if (waitSel && waitSel !== '-') { try { await page.waitForSelector(waitSel, { timeout: 60000 }); } catch (e) { console.log('waitSel timeout:', waitSel); } }
await page.waitForTimeout(Number(extraMs || 3000));
await page.screenshot({ path: out });
console.log('saved', out, 'url:', page.url());
await browser.close();
