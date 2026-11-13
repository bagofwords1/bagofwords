// Ask the agent for the sales dashboard; the MCP App should render in chat.
const { launch, shot, api, BASE } = require('./common');
const fs = require('fs');
const ids = JSON.parse(fs.readFileSync(__dirname + '/ids.json'));
const PROMPT = process.argv[2] || 'Show me the regional sales dashboard for Q3.';
const TAG = process.argv[3] || '04';
(async () => {
  const { browser, page } = await launch({ dsf: Number(process.env.DSF || 1) });
  await page.goto(BASE + '/settings/models', { waitUntil: 'commit', timeout: 180000 });
  await page.waitForTimeout(2500);
  const rep = await api(page, 'POST', '/api/reports', { title: 'Regional sales review', data_sources: [ids.data_source_id] });
  console.log('report', rep.status, rep.body && rep.body.id);
  const rid = rep.body.id;
  fs.writeFileSync(__dirname + '/report.json', JSON.stringify({ id: rid }));
  await page.goto(`${BASE}/reports/${rid}`, { waitUntil: 'commit', timeout: 180000 });
  const input = page.locator('[contenteditable="true"]').first();
  await input.waitFor({ timeout: 180000 });
  await page.waitForTimeout(1500);
  await input.click();
  await page.keyboard.type(PROMPT);
  await page.waitForTimeout(500);
  await page.locator('button[class*="rounded-full"]').last().click();
  // wait for the run to finish
  await page.waitForTimeout(4000);
  for (let i = 0; i < 120; i++) {
    const busy = await page.locator('[data-testid="stop-button"]').count();
    if (!busy) break;
    await page.waitForTimeout(1000);
  }
  await page.waitForTimeout(3000);
  await shot(page, `${TAG}a_after_run`);
  const app = page.locator('[data-testid="mcp-app-frame"]');
  console.log('mcp-app frames:', await app.count());
  if (await app.count()) {
    await app.first().scrollIntoViewIfNeeded();
    await page.waitForTimeout(2500);
    await shot(page, `${TAG}b_app`);
    const box = await app.first().boundingBox();
    console.log('frame box', JSON.stringify(box));
  }
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
