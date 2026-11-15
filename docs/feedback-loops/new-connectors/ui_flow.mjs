// UI evidence for the new connectors: the schema-generated connect form (filled
// in, Test Connection succeeding) for several types, and agent answers as the
// report page renders them. Usage: node ui_flow.mjs <report-id>...
import { createRequire } from 'node:module';
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');
const OUT = process.env.SHOTS_DIR || '/tmp/bow-connector-shots';
const reports = process.argv.slice(2);

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const ctx = await browser.newContext({ storageState: `${OUT}/state.json`, viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });
let page = await ctx.newPage();
page.setDefaultTimeout(60000);

const FORMS = [
  { tile: 'Apache Cassandra', file: 'form-cassandra', name: 'Shop (Cassandra)', fields: { contact_points: '127.0.0.1' } },
  { tile: 'Apache Iceberg', file: 'form-iceberg', name: 'Lakehouse (Iceberg)', fields: { uri: 'http://127.0.0.1:8181' }, auth: 'No Authentication' },
  { tile: 'Google Cloud Storage', file: 'form-gcs', name: 'Reports bucket (GCS)', fields: { bucket: 'bow-test', endpoint_url: 'http://127.0.0.1:4443', project: 'test' }, auth: 'Application Default Credentials' },
  { tile: 'Dremio', file: 'form-dremio', name: 'Lakehouse (Dremio)', fields: { url: 'http://127.0.0.1:9047' }, auth: 'Username / Password', creds: { username: 'admin', password: 'Passw0rd!x' } },
];

async function openModal() {
  await page.close().catch(() => {});
  page = await ctx.newPage();
  page.setDefaultTimeout(60000);
  page.on('dialog', (d) => d.accept().catch(() => {}));
  await page.goto('http://localhost:3000/agents/new');
  await page.waitForTimeout(2500);
  if (!(await page.getByText('Add Connection').count())) await page.getByText('Create new connection').click();
  const modal = page.locator('[id^=headlessui-dialog-panel]').last();
  await modal.waitFor();
  return modal;
}

const only = process.env.ONLY ? process.env.ONLY.split(',') : null;
for (const f of FORMS.filter((x) => !only || only.includes(x.file))) {
  let modal;
  try { modal = await openModal(); } catch (e) { await page.screenshot({ path: `${OUT}/debug-${f.file}.png` }); throw e; }
  await modal.locator('input').first().fill(f.tile.split(' ').pop());
  await page.waitForTimeout(500);
  await modal.getByText(f.tile, { exact: true }).first().click();
  await page.waitForTimeout(1500);
  if (f.auth) {
    await modal.locator('.connection-auth-selector button').first().click();
    await page.getByRole('option', { name: f.auth }).first().click();
    await page.waitForTimeout(800);
  }
  await modal.locator('#connection-name').fill(f.name);
  for (const [k, v] of Object.entries(f.fields)) await modal.locator(`#${k}`).first().fill(v);
  for (const [k, v] of Object.entries(f.creds || {})) await modal.locator(`#${k}`).last().fill(v);
  await modal.screenshot({ path: `${OUT}/${f.file}.png` });
  await modal.getByRole('button', { name: /^Connect$/ }).last().click();
  await page.waitForTimeout(12000);
  await modal.screenshot({ path: `${OUT}/${f.file}-tested.png` });
  console.log('form', f.file);
  await page.keyboard.press('Escape');
}

for (const [i, rid] of reports.entries()) {
  await page.goto(`http://localhost:3000/reports/${rid}`);
  await page.waitForTimeout(9000);
  await page.screenshot({ path: `${OUT}/answer-${i + 1}.png` });
  console.log('answer', rid);
}
await browser.close();
