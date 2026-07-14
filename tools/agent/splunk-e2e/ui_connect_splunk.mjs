// Connect Splunk through the real onboarding UI, with screenshots per step.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = '/home/user/bagofwords/media/pr/ai-ecstatic-sagan-84i4pc';
const BASE = 'http://localhost:3000';
mkdirSync(OUT, { recursive: true });

const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const ctx = await b.newContext({ viewport: { width: 1512, height: 950 }, storageState: '/home/user/bagofwords/media/pr/state.json' });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (n) => { await page.screenshot({ path: `${OUT}/${n}.png` }); console.log('shot', n); };

// onboarding welcome -> data step
await page.goto(`${BASE}/`, { waitUntil: 'commit' });
await page.getByRole('button', { name: 'Next' }).click();
await page.getByRole('button', { name: 'Splunk', exact: true }).waitFor();
await page.waitForTimeout(800);
await shot('03-onboarding-connectors');

// pick Splunk
await page.getByRole('button', { name: 'Splunk', exact: true }).click();
await page.locator('#host').waitFor();
await page.waitForTimeout(500);
await shot('04-splunk-form-empty');

// fill config
const nameInput = page.locator('input[placeholder*="Sales DB"]').first();
await nameInput.fill('Splunk (docker)');
await page.locator('#host').fill('localhost');
// turn OFF Verify SSL (self-signed container cert) — first switch in the form
const verifySwitch = page.locator('button[role=switch]').first();
console.log('verify_ssl before:', await verifySwitch.getAttribute('aria-checked'));
await verifySwitch.click();
console.log('verify_ssl after:', await verifySwitch.getAttribute('aria-checked'));

// auth variant -> Username / Password
await page.getByRole('button', { name: /Authentication Token/ }).last().click();
await page.getByRole('option', { name: /Username \/ Password/ }).click();
await page.locator('#username').waitFor();
await page.locator('#username').fill('admin');
await page.locator('#password').fill(process.env.SPLUNK_PASSWORD || 'BowSplunk123!');
await page.waitForTimeout(300);
await shot('05-splunk-form-filled');

// test connection
await page.getByRole('button', { name: /Test Connection/i }).click();
await page.getByText(/Connected successfully/i).waitFor({ timeout: 90000 });
await shot('06-test-connection-ok');

// submit
await page.locator('button[type=submit]').first().click();
await page.waitForURL(/onboarding\/data\/.+\/schema/, { timeout: 60000 });
console.log('schema URL:', page.url());
await page.waitForTimeout(2000);
await shot('07-schema-indexing');

// wait for schema discovery to finish: the three index::sourcetype tables
await page.getByText(/web::access_combined/).waitFor({ timeout: 300000 });
await page.waitForTimeout(1000);
await shot('08-schema-tables');
const btns = await page.locator('button').allTextContents();
console.log('buttons:', JSON.stringify(btns.map(t => t.trim()).filter(Boolean)));
await ctx.close(); await b.close(); console.log('CONNECT DONE');
