#!/usr/bin/env node
const { chromium } = require('/home/user/bagofwords/frontend/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  });
  for (const code of ['en', 'es', 'he']) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    await ctx.addInitScript((c) => {
      try { localStorage.setItem('bow.locale', c); } catch {}
    }, code);
    const page = await ctx.newPage();
    await page.goto('http://localhost:3000/i18n-smoke', { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(1500);
    const info = await page.evaluate(() => ({
      dir: document.documentElement.getAttribute('dir'),
      lang: document.documentElement.getAttribute('lang'),
      hello: document.querySelector('[data-test="smoke-hello"]')?.textContent?.trim(),
      loc: document.querySelector('[data-test="smoke-locale"]')?.textContent?.trim(),
    }));
    console.log(JSON.stringify({ code, ...info }));
    await page.screenshot({ path: `/tmp/smoke_${code}.png`, fullPage: true });
    await ctx.close();
  }
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
