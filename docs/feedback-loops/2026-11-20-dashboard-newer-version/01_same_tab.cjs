// Repro: create a dashboard in chat, then ask to add to it; inspect the pane's version picker.
// usage: node 01_repro.cjs <outdir> [reportId]
const { chromium } = require('playwright');
const fs = require('fs');
const out = process.argv[2] || 'shots';
const existing = process.argv[3];
fs.mkdirSync(out, { recursive: true });
const O = 'http://localhost:3000';
const log = fs.createWriteStream(`${out}/api.jsonl`);

async function send(page, text) {
  const box = page.locator('[contenteditable="true"]').first();
  await box.click();
  await box.fill(text);
  await page.waitForTimeout(500);
  const bb = await box.boundingBox();
  const btns = page.locator('button[class*="rounded-full"]');
  const n = await btns.count();
  let best = null, bd = 1e9;
  for (let i = 0; i < n; i++) {
    const b = await btns.nth(i).boundingBox();
    if (!b) continue;
    const d = Math.abs(b.x - (bb.x + bb.width)) + Math.abs(b.y - (bb.y + bb.height));
    if (b.x > bb.x && d < bd) { bd = d; best = btns.nth(i); }
  }
  await best.click();
}
async function waitDone(page, tag, max = 400000) {
  const t0 = Date.now();
  await page.locator('[data-testid="stop-button"]').first().waitFor({ timeout: 30000 }).catch(() => console.log(tag, 'no stop button seen'));
  while (Date.now() - t0 < max) {
    const stop = await page.locator('[data-testid="stop-button"]').count();
    if (!stop) break;
    await page.waitForTimeout(3000);
  }
  await page.waitForTimeout(3000);
  console.log(tag, 'done in', Math.round((Date.now() - t0) / 1000), 's');
}
async function pickerState(page) {
  const picker = page.locator('[data-testid="frame-version-picker"]');
  if (!(await picker.count())) return { picker: false };
  return { picker: true, label: (await picker.innerText()).replace(/\s+/g, ' ').trim() };
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } });
  const page = await ctx.newPage();
  page.on('response', async r => { if (r.url().includes('/api/')) log.write(JSON.stringify({ t: Date.now(), m: r.request().method(), s: r.status(), u: r.url() }) + '\n'); });
  page.on('console', m => { if (/DBG/.test(m.text())) console.log('[console]', m.text()); });
  await page.goto(`${O}/users/sign-in`);
  await page.fill('#email', 'admin@example.com');
  await page.fill('#password', 'Password123!');
  await page.click('button[type=submit]');
  await page.waitForTimeout(3000);
  if (page.url().includes('/onboarding')) { await page.getByText('Skip onboarding').click(); await page.waitForTimeout(2000); }
  await ctx.storageState({ path: `${out}/state.json` });

  if (existing) {
    await page.goto(`${O}/reports/${existing}`);
    await page.waitForSelector('[contenteditable="true"]', { timeout: 120000 });
  } else {
    await page.goto(`${O}/`);
    await page.waitForSelector('[contenteditable="true"]', { timeout: 120000 });
    await page.waitForTimeout(2000);
    await send(page, 'Create a simple dashboard with one bar chart: top 5 artists by number of tracks. Keep it minimal.');
    await page.waitForURL(/\/reports\//, { timeout: 60000 });
    console.log('report', page.url());
    await waitDone(page, 'turn1');
    await page.screenshot({ path: `${out}/01_after_create.png` });
    console.log('picker after create', await pickerState(page));
  }

  // open the dashboard via its card in the chat (the "dashboard link")
  await page.getByText('Top 5 Artists by Tracks', { exact: true }).first().click().catch(() => {});
  await page.waitForSelector('[data-testid="frame-version-picker"]', { timeout: 60000 });
  await page.waitForTimeout(4000);
  await page.screenshot({ path: `${out}/01b_pane_open.png` });
  console.log('picker after open', await pickerState(page));
  await send(page, process.env.TURN2 || 'Add a second chart to the dashboard: number of tracks per genre (top 5) as a bar chart.');
  await waitDone(page, 'turn2');
  await page.screenshot({ path: `${out}/02_after_add.png` });
  console.log('picker after add', await pickerState(page));
  console.log('overlay after add', await page.locator('[data-testid="newer-version-overlay"]').count());
  const picker = page.locator('[data-testid="frame-version-picker"]');
  if (await picker.count()) {
    await picker.click();
    await page.waitForTimeout(800);
    const rows = await page.locator('[data-testid="frame-version-row"]').allInnerTexts();
    console.log('picker rows', rows.map(r => r.replace(/\s+/g, ' ')));
    await page.screenshot({ path: `${out}/03_picker_open.png` });
    await page.keyboard.press('Escape');
  }
  // compare with server truth
  const rid = page.url().match(/reports\/([^/?#]+)/)[1];
  const list = await page.evaluate(async rid => {
    const tok = decodeURIComponent((document.cookie.match(/auth\.token=([^;]+)/) || [])[1] || '');
    const orgs = await (await fetch('/api/organizations', { headers: { Authorization: `Bearer ${tok}` } })).json();
    const r = await fetch(`/api/artifacts/report/${rid}`, { headers: { Authorization: `Bearer ${tok}`, 'X-Organization-Id': orgs[0].id } });
    return (await r.json()).map(a => ({ v: a.version, id: a.id, status: a.status }));
  }, rid);
  console.log('server versions', JSON.stringify(list));
  fs.writeFileSync(`${out}/report_id`, rid);
  await browser.close();
})();
