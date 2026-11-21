// Scenarios where the open dashboard may go stale.
// usage: SCEN=A|B|C node 03_scenarios.cjs <outdir> <reportId>
const { chromium } = require('playwright');
const fs = require('fs');
const [out, rid] = process.argv.slice(2);
const SCEN = process.env.SCEN || 'C';
fs.mkdirSync(out, { recursive: true });
const O = 'http://localhost:3000';

async function send(page, text) {
  const box = page.locator('[contenteditable="true"]').first();
  await box.click(); await box.fill(text); await page.waitForTimeout(500);
  const bb = await box.boundingBox();
  const btns = page.locator('button[class*="rounded-full"]');
  let best = null, bd = 1e9;
  for (let i = 0; i < await btns.count(); i++) {
    const b = await btns.nth(i).boundingBox(); if (!b) continue;
    const d = Math.abs(b.x - (bb.x + bb.width)) + Math.abs(b.y - (bb.y + bb.height));
    if (b.x > bb.x && d < bd) { bd = d; best = btns.nth(i); }
  }
  await best.click();
}
async function waitDone(page, tag) {
  const t0 = Date.now();
  await page.locator('[data-testid="stop-button"]').first().waitFor({ timeout: 30000 }).catch(() => console.log(tag, 'no stop seen'));
  while (Date.now() - t0 < 400000 && await page.locator('[data-testid="stop-button"]').count()) await page.waitForTimeout(2000);
  await page.waitForTimeout(3000);
  console.log(tag, 'done', Math.round((Date.now() - t0) / 1000), 's');
}
async function picker(page) {
  const p = page.locator('[data-testid="frame-version-picker"]');
  if (!(await p.count())) return 'no picker';
  const label = (await p.innerText()).replace(/\s+/g, ' ').trim();
  await p.click(); await page.waitForTimeout(600);
  const rows = (await page.locator('[data-testid="frame-version-row"]').allInnerTexts()).map(r => r.replace(/\s+/g, ' '));
  await page.keyboard.press('Escape');
  return { label, rows };
}
async function openPaneViaCard(page) {
  await page.getByText('Top 5 Artists by Tracks', { exact: true }).first().click();
  await page.waitForSelector('[data-testid="frame-version-picker"]', { timeout: 60000 });
  await page.waitForTimeout(3000);
}
async function serverVersions(page) {
  return page.evaluate(async rid => {
    const tok = decodeURIComponent((document.cookie.match(/auth\.token=([^;]+)/) || [])[1] || '');
    const orgs = await (await fetch('/api/organizations', { headers: { Authorization: `Bearer ${tok}` } })).json();
    const r = await fetch(`/api/artifacts/report/${rid}`, { headers: { Authorization: `Bearer ${tok}`, 'X-Organization-Id': orgs[0].id } });
    return (await r.json()).map(a => a.version);
  }, rid);
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ storageState: process.env.STATE || 'state.json', viewport: { width: 1600, height: 950 } });
  const a = await ctx.newPage();
  a.on('console', m => { if (/DBG/.test(m.text())) console.log('[c]', m.text().slice(0,300)); });
  await a.goto(`${O}/reports/${rid}`);
  await a.waitForSelector('[contenteditable="true"]', { timeout: 120000 });
  await a.waitForTimeout(3000);
  const prompt = process.env.PROMPT || 'Add a KPI card to the top of the dashboard showing the total number of tracks.';
  if (SCEN === 'E') {
    // Tab A has the pane open but never hears the run (its stream is cut:
    // a dropped connection / a run it is not attached to). Tab B edits.
    await a.route('**/completions/*/stream', r => r.abort());
    await a.route('**/reports/activity/stream', r => r.abort());
    await a.waitForSelector('[data-testid="frame-version-picker"]', { timeout: 60000 });
    console.log('A before', JSON.stringify(await picker(a)));
    await a.screenshot({ path: `${out}/E1_tabA_before.png` });
    const b = await ctx.newPage();
    await b.goto(`${O}/reports/${rid}`);
    await b.waitForSelector('[contenteditable="true"]', { timeout: 120000 });
    await b.waitForTimeout(3000);
    await send(b, prompt);
    await waitDone(b, 'tabB');
    console.log('server', await serverVersions(a));
    await a.bringToFront();
    await a.waitForTimeout(Number(process.env.WAIT || 18000));
    await a.screenshot({ path: `${out}/E2_tabA_after.png` });
    const ov = a.locator('[data-testid="newer-version-overlay"]');
    console.log('overlay visible', await ov.isVisible().catch(() => false));
    console.log('A after', JSON.stringify(await picker(a)));
    await a.screenshot({ path: `${out}/E3_tabA_picker.png` });
    if (await ov.count()) {
      await a.locator('[data-testid="newer-version-refresh"]').click();
      await a.waitForTimeout(6000);
      await a.screenshot({ path: `${out}/E4_tabA_refreshed.png` });
      console.log('A refreshed', JSON.stringify(await picker(a)), 'overlay', await ov.count());
    }
  } else if (SCEN === 'C') {
    await openPaneViaCard(a);
    console.log('A before', JSON.stringify(await picker(a)));
    await a.screenshot({ path: `${out}/C1_tabA_before.png` });
    const b = await ctx.newPage();
    await b.goto(`${O}/reports/${rid}`);
    await b.waitForSelector('[contenteditable="true"]', { timeout: 120000 });
    await b.waitForTimeout(3000);
    await send(b, prompt);
    await waitDone(b, 'tabB');
    await a.waitForTimeout(5000);
    console.log('server', await serverVersions(a));
    console.log('A after', JSON.stringify(await picker(a)));
    await a.screenshot({ path: `${out}/C2_tabA_after.png` });
  } else if (SCEN === 'D') {
    // the dashboard opened on its own page (Open in new tab / Dashboards list)
    const dash = await a.evaluate(async rid => {
      const tok = decodeURIComponent((document.cookie.match(/auth\.token=([^;]+)/) || [])[1] || '');
      const orgs = await (await fetch('/api/organizations', { headers: { Authorization: `Bearer ${tok}` } })).json();
      const r = await fetch(`/api/artifacts/report/${rid}`, { headers: { Authorization: `Bearer ${tok}`, 'X-Organization-Id': orgs[0].id } });
      return (await r.json())[0].artifact_id;
    }, rid);
    const d = await ctx.newPage();
    d.on('console', m => { if (/DBG/.test(m.text())) console.log('[d]', m.text().slice(0, 300)); });
    await d.goto(`${O}/d/${dash}/draft`);
    await d.waitForTimeout(12000);
    await d.screenshot({ path: `${out}/D1_draft_before.png` });
    console.log('draft before', await d.evaluate(() => document.body.innerText.slice(0, 200).replace(/\s+/g, ' ')));
    await send(a, prompt);
    await waitDone(a, 'D');
    console.log('server', await serverVersions(a));
    await d.waitForTimeout(Number(process.env.WAIT || 5000));
    await d.screenshot({ path: `${out}/D2_draft_after.png` });
    console.log('draft after', await d.evaluate(() => document.body.innerText.slice(0, 300).replace(/\s+/g, ' ')));
    const ov = d.locator('[data-testid="newer-version-overlay"]');
    console.log('overlay visible', await ov.isVisible().catch(() => false));
    if (await ov.count()) {
      await d.locator('[data-testid="newer-version-refresh"]').click();
      await d.waitForTimeout(10000);
      await d.screenshot({ path: `${out}/D3_draft_refreshed.png` });
      console.log('after refresh', await d.evaluate(() => document.body.innerText.slice(0, 200).replace(/\s+/g, ' ')), 'overlay', await ov.count());
    }
  } else if (SCEN === 'A') {
    await openPaneViaCard(a);
    console.log('before', JSON.stringify(await picker(a)));
    await a.locator('[data-artifact-frame]').count();
    // close pane with the frame's X ("back to chat")
    await a.locator('button:has(.iconify[class*="x-mark"]), button:has([class*="x-mark"])').first().click().catch(() => {});
    await a.waitForTimeout(1500);
    await send(a, prompt);
    await waitDone(a, 'A');
    await a.screenshot({ path: `${out}/A1_after_run.png` });
    console.log('server', await serverVersions(a));
    console.log('after (no reopen)', JSON.stringify(await picker(a)));
  } else if (SCEN === 'B') {
    await openPaneViaCard(a);
    await a.getByRole('button', { name: 'Summary', exact: true }).last().click();
    await a.waitForTimeout(1500);
    await send(a, prompt);
    await waitDone(a, 'B');
    await a.getByRole('button', { name: 'Dashboard', exact: true }).last().click();
    await a.waitForTimeout(4000);
    await a.screenshot({ path: `${out}/B1_after_tab.png` });
    console.log('server', await serverVersions(a));
    console.log('after tab', JSON.stringify(await picker(a)));
  }
  await browser.close();
})();
