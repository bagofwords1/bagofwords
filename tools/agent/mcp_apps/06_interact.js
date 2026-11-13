// Drive every host feature from inside the app and capture each step.
const { launch, shot, BASE, DIR } = require('./common');
const fs = require('fs');
const { id } = JSON.parse(fs.readFileSync(__dirname + '/report.json'));
const P = 'shots/06';
async function appShot(page, name) {
  const el = page.locator('[data-testid="mcp-app"]').first();
  await el.scrollIntoViewIfNeeded();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${DIR}/${P}_${name}.png`, clip: await (async () => { const b = await el.boundingBox(); return { x: Math.max(0, b.x - 24), y: Math.max(0, b.y - 64), width: b.width + 48, height: Math.min(b.height + 96, 940) }; })() });
}
(async () => {
  const { browser, page } = await launch({ dsf: Number(process.env.DSF || 2) });
  await page.goto(`${BASE}/reports/${id}`, { waitUntil: 'commit', timeout: 180000 });
  const app = page.locator('[data-testid="mcp-app-frame"]').first();
  await app.waitFor({ timeout: 180000 });
  await page.waitForTimeout(4000);
  const inner = () => page.frameLocator('[data-testid="mcp-app-frame"]').first().frameLocator('iframe');
  await inner().locator('.row').first().waitFor({ timeout: 30000 });
  await appShot(page, '1_rendered');

  // 1. app-only tool through the host (refresh_sales, read-only → no prompt)
  await inner().locator('#quarter').selectOption('Q1');
  await inner().locator('#status').filter({ hasText: 'Refreshed Q1' }).waitFor({ timeout: 30000 });
  await appShot(page, '2_refreshed_q1');

  // 2. model context: click a bar
  await inner().locator('.row[data-region="APAC"]').click();
  await inner().locator('#status').filter({ hasText: 'Shared selection' }).waitFor({ timeout: 30000 });
  await appShot(page, '3_selected_apac');

  // 3. write-capable app-only tool → BOW asks first
  await inner().locator('#followup').click();
  await page.locator('[data-testid="mcp-app-confirm"]').waitFor({ timeout: 30000 });
  await appShot(page, '4_confirm_tool');
  await page.locator('[data-testid="mcp-app-confirm-allow"]').click();
  await inner().locator('#status').filter({ hasText: 'Created follow-up' }).waitFor({ timeout: 30000 });
  await appShot(page, '5_followup_created');

  // 4. open-link → BOW asks first; cancel
  await inner().locator('#docs').click();
  await page.locator('[data-testid="mcp-app-confirm"]').waitFor({ timeout: 30000 });
  await appShot(page, '6_confirm_link');
  await page.locator('[data-testid="mcp-app-confirm-deny"]').click();
  await inner().locator('#status').filter({ hasText: 'Link not opened' }).waitFor({ timeout: 30000 });

  // 5. fullscreen and back
  await inner().locator('#fullscreen').click();
  await page.locator('[data-testid="mcp-app-exit-fullscreen"]').waitFor({ timeout: 30000 });
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${DIR}/${P}_7_fullscreen.png` });
  await page.locator('[data-testid="mcp-app-exit-fullscreen"]').click();
  await page.waitForTimeout(800);

  // 6. ui/message → a new turn in the conversation
  await inner().locator('#ask').click();
  await page.waitForTimeout(3000);
  for (let i = 0; i < 120; i++) { if (!(await page.locator('[data-testid="stop-button"]').count())) break; await page.waitForTimeout(1000); }
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${DIR}/${P}_8_message_turn.png` });
  console.log('done');
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
