const { chromium } = require('playwright');
const fs = require('fs');
const BASE = 'http://localhost:3000';
async function open(name, { state = true } = {}) {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1,
    storageState: state && fs.existsSync('state.json') ? 'state.json' : undefined });
  const page = await ctx.newPage();
  const log = fs.createWriteStream(`http_${name}.jsonl`, { flags: 'a' });
  page.on('response', async r => { if (r.url().includes('/api/')) log.write(JSON.stringify({ t: Date.now(), m: r.request().method(), u: r.url().replace(BASE, ''), s: r.status() }) + '\n'); });
  page.on('console', m => { if (m.type() === 'error') log.write(JSON.stringify({ console: m.text().slice(0, 300) }) + '\n'); });
  const shot = async (n) => page.screenshot({ path: `shots/${name}_${n}.png` });
  return { browser, ctx, page, shot, BASE };
}
module.exports = { open, BASE };
