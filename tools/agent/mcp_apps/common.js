const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const DIR = __dirname;
const BASE = 'http://localhost:3000';
async function launch({ state = true, viewport = { width: 1400, height: 950 }, dsf = 1, colorScheme = 'light' } = {}) {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
  const opts = { viewport, deviceScaleFactor: dsf, colorScheme };
  if (state && fs.existsSync(path.join(DIR, 'state.json'))) opts.storageState = path.join(DIR, 'state.json');
  const context = await browser.newContext(opts);
  const page = await context.newPage();
  const log = fs.createWriteStream(path.join(DIR, 'http.jsonl'), { flags: 'a' });
  page.on('response', async (r) => { if (r.url().includes('/api/')) log.write(JSON.stringify({ t: Date.now(), m: r.request().method(), u: r.url(), s: r.status() }) + '\n'); });
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) fs.appendFileSync(path.join(DIR, 'console.log'), `[${m.type()}] ${m.text()}\n`); });
  page.on('pageerror', (e) => fs.appendFileSync(path.join(DIR, 'console.log'), `[pageerror] ${e.message}\n`));
  return { browser, context, page };
}
async function shot(page, name) { await page.screenshot({ path: path.join(DIR, 'shots', name + '.png') }); }
async function api(page, method, url, body) {
  return await page.evaluate(async ({ method, url, body }) => {
    const tok = (document.cookie.split('; ').find(c => c.startsWith('auth.token=')) || '').split('=')[1];
    const jwt = decodeURIComponent(tok || '').replace(/^Bearer\s+/, '');
    const orgs = await (await fetch('/api/organizations', { headers: { Authorization: 'Bearer ' + jwt } })).json();
    const org = (Array.isArray(orgs) ? orgs : orgs.organizations || [])[0];
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + jwt, 'X-Organization-Id': org.id }, body: body ? JSON.stringify(body) : undefined });
    let j = null; try { j = await r.json(); } catch {}
    return { status: r.status, body: j, org: org.id };
  }, { method, url, body });
}
module.exports = { launch, shot, api, BASE, DIR };
