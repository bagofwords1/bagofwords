// Nested custom metrics must retain independently clickable source details.
const fs = require('fs'), path = require('path'), cp = require('child_process'), assert = require('assert/strict');
const root = path.resolve(__dirname, '../..');
const { chromium } = require(path.join(root, 'frontend/node_modules/playwright'));
const baseline = process.argv.includes('--baseline');
const rtl = process.argv.includes('--rtl');
const source = baseline ? cp.execFileSync('git', ['show', '3e32c09db2e88815bd2b9910db84ddd71d145d7f:frontend/public/libs/artifact-globals.js'], { cwd: root, encoding: 'utf8' }) : fs.readFileSync(path.join(root, 'frontend/public/libs/artifact-globals.js'), 'utf8');
let browser;
(async () => {
  browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 960, height: 500 } });
  await page.setContent('<html lang="' + (rtl ? 'he' : 'en') + '" dir="' + (rtl ? 'rtl' : 'ltr') + '"><body><div id="root"></div></body></html>');
  for (const file of ['react-18.production.min.js', 'react-dom-18.production.min.js']) await page.addScriptTag({ path: path.join(root, 'frontend/public/libs', file) });
  await page.evaluate(() => { window.ARTIFACT_DATA = { runtime: { version: 11 }, visualizations: [{ id: 'fixture', title: 'Orders', rows: [{ revenue: 420 }], columns: [{ field: 'revenue', headerName: 'Revenue' }] }] }; });
  await page.addScriptTag({ content: source });
  await page.evaluate(() => ReactDOM.createRoot(document.getElementById('root')).render(React.createElement('section', { 'data-bow-viz': 'fixture', style: { margin: 40, background: '#f3f4f6', width: 450, height: 180 } }, React.createElement('div', { 'data-bow-viz': 'fixture', 'data-bow-calc': 'SUM(revenue)', style: { width: 450, height: 180 } }, React.createElement('h1', { style: { margin: 0, padding: 32 } }, document.documentElement.dir === 'rtl' ? 'הכנסות ממקור הנתונים' : 'Source-backed revenue')))));
  await page.locator('[data-bow-ibtn]').nth(1).waitFor();
  await page.screenshot({ path: path.join(root, 'media/pr/data-app-generation/provenance-' + (baseline ? 'before' : 'after') + (rtl ? '-he' : '') + '.png') });
  const markers = page.locator('[data-bow-ibtn]');
  assert.equal(await markers.count(), 2);
  for (let i = 0; i < 2; i++) {
    await markers.nth(i).click({ timeout: 2000 });
    await page.locator('[data-bow-panel]').waitFor();
    await page.locator('body').press('Escape');
    await page.locator('[data-bow-panel]').waitFor({ state: 'detached' });
  }
  console.log('PASS: nested provenance controls are independently clickable');
})().catch(error => { console.error(error.message); process.exitCode = 1; }).finally(async () => { if (browser) await browser.close(); });
