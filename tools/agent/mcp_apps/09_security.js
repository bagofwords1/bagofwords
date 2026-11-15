const { launch, BASE } = require('./common');
const fs = require('fs');
const { id } = JSON.parse(fs.readFileSync(__dirname + '/report.json'));
(async () => {
  const { browser, page } = await launch({ dsf: 1 });
  // 1. Normal mode: the app frame is opaque-origin and cannot touch BOW.
  await page.goto(`${BASE}/reports/${id}`, { waitUntil: 'commit', timeout: 180000 });
  const frameEl = page.locator('[data-testid="mcp-app-frame"]').first();
  await frameEl.waitFor({ timeout: 180000 });
  console.log('sandbox attr:', await frameEl.getAttribute('sandbox'));
  const inner = page.frameLocator('[data-testid="mcp-app-frame"]').first().frameLocator('iframe');
  await inner.locator('.row').first().waitFor({ timeout: 60000 });
  const proxy = page.frames().find(f => f.url().includes('/api/mcp_apps/sandbox'));
  const app = proxy.childFrames()[0];
  console.log('app origin:', await app.evaluate(() => self.origin));
  console.log('app reads BOW localStorage:', await app.evaluate(() => { try { return Object.keys(window.top.localStorage).length } catch (e) { return 'blocked: ' + e.name } }));
  console.log('app reads BOW DOM:', await app.evaluate(() => { try { return !!window.top.document.title } catch (e) { return 'blocked: ' + e.name } }));
  console.log('app fetches BOW API:', await app.evaluate(async () => { try { const r = await fetch('/api/users/whoami'); return r.status } catch (e) { return 'blocked: ' + e.message.slice(0, 40) } }));
  console.log('app fetches the internet:', await app.evaluate(async () => { try { await fetch('https://example.com'); return 'reached' } catch (e) { return 'blocked' } }));
  // 2. Attack: a BOW-origin page frames the proxy WITHOUT a sandbox attribute.
  const res = await page.evaluate(async () => new Promise((resolve) => {
    const f = document.createElement('iframe');
    f.src = '/api/mcp_apps/sandbox';
    f.onload = () => { try { resolve(f.contentDocument.body.innerText) } catch (e) { resolve('err ' + e.message) } };
    document.body.appendChild(f);
  }));
  console.log('unsandboxed same-origin proxy says:', JSON.stringify(res));
  // 3. Opened top-level.
  await page.goto(`${BASE}/api/mcp_apps/sandbox`);
  console.log('top-level proxy says:', JSON.stringify(await page.locator('body').innerText()));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
