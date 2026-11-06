const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const DIR = process.env.PW_OUT || __dirname;
const BASE = 'http://localhost:3000';
async function launch({ state = true } = {}) {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
  const opts = { viewport: { width: 1400, height: 900 } };
  if (state && fs.existsSync(path.join(DIR, 'state.json'))) opts.storageState = path.join(DIR, 'state.json');
  const context = await browser.newContext(opts);
  const page = await context.newPage();
  const log = fs.createWriteStream(path.join(DIR, 'api.jsonl'), { flags: 'a' });
  page.on('response', r => { if (r.url().includes('/api/') || r.url().includes('/thumbnails/')) log.write(JSON.stringify({ t: Date.now(), m: r.request().method(), u: r.url(), s: r.status() }) + '\n'); });
  return { browser, context, page };
}
let n = 0;
async function shot(page, name) { n++; const f = path.join(DIR, 'shots', `${String(n).padStart(2, '0')}_${name}.png`); await page.screenshot({ path: f }); return f; }
module.exports = { launch, shot, BASE, DIR };
