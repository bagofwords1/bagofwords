// Add Anthropic provider with ONLY Claude Haiku enabled, via /settings/models.
const { launch, shot } = require('./common');
const KEY = process.env.BOW_TEST_LLM_KEY;
(async () => {
  const { browser, page } = await launch();
  await page.goto('http://localhost:3000/settings/models', { waitUntil: 'commit', timeout: 180000 });
  await page.waitForTimeout(6000);
  await shot(page, '02a_models');
  for (const label of ['Integrate Models', 'Add Provider', 'New Provider']) {
    const b = page.getByRole('button', { name: label });
    if (await b.count()) { await b.first().click(); await page.waitForTimeout(1500); }
  }
  await page.locator('img[alt="anthropic logo"]').first().click();
  await page.waitForTimeout(1500);
  await page.locator('input[placeholder*="provider name" i]').first().fill('Anthropic-Haiku');
  // API key: the empty input without a placeholder
  const keyInput = page.locator('xpath=//*[normalize-space(text())="Api Key"]/following::input[1]').first();
  await keyInput.fill(KEY);
  await page.waitForTimeout(800);
  // Model checkboxes: keep only Haiku 4.5 checked
  const boxes = page.locator('input[type=checkbox]');
  const count = await boxes.count();
  const names = [];
  for (let i = 0; i < count; i++) {
    const cb = boxes.nth(i);
    const label = await cb.evaluate((el) => { let n = el; for (let k = 0; k < 8 && n; k++) { n = n.parentElement; if (n && n.innerText && n.innerText.includes('Model ID:')) return n.innerText; } return ''; });
    if (!label) continue;
    const want = /Claude Haiku 5\.5/.test(label);
    names.push((want ? '[x] ' : '[ ] ') + label.split('\n')[0]);
    if ((await cb.isChecked()) !== want) await cb.click({ force: true });
  }
  console.log(names.join('\n'));
  await shot(page, '02b_provider_form');
  const test = page.getByRole('button', { name: /Test Connection/i });
  if (await test.count()) { await test.first().click(); await page.waitForTimeout(8000); }
  await shot(page, '02c_tested');
  await page.getByRole('button', { name: /Save Provider/i }).first().click();
  await page.waitForTimeout(4000);
  await shot(page, '02d_saved');
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
