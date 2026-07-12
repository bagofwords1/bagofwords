// Playwright UI capture: log in, open connection creation, screenshot the
// network_dir + S3 config forms showing the new include_globs + index_mode fields.
import { createRequire } from 'module';
const require = createRequire('/home/user/bagofwords/frontend/');
const { chromium } = require('playwright');
import fs from 'fs';

const OUT = '/home/user/bagofwords/.agents/sandbox-feedback-loop/shots';
fs.mkdirSync(OUT, { recursive: true });
const BASE = 'http://localhost:3000';

const shot = async (page, name) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true });
  console.log('shot:', name);
};

const run = async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(20000);

  // --- login ---
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  await page.locator('#email').fill('admin@example.com');
  await page.locator('#password').fill('Password123!');
  await shot(page, '00-signin');
  await page.locator('button[type="submit"]').first().click();
  try {
    await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 });
  } catch (e) { console.log('login nav wait timed out; url=', page.url()); }
  await page.waitForTimeout(2500);
  console.log('after login url:', page.url());
  await shot(page, '01-after-login');

  // --- connection creation flow ---
  await page.goto(`${BASE}/agents/new`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);
  await shot(page, '02-agents-new');

  // Try to open the add-connection picker (button text varies).
  const openers = ['Add connection', 'Add Connection', 'New connection', 'Connect a data source', 'Add data source', 'Connect'];
  for (const t of openers) {
    const b = page.getByText(t, { exact: false }).first();
    if (await b.count().catch(() => 0)) {
      try { await b.click({ timeout: 3000 }); console.log('clicked opener:', t); break; } catch (e) {}
    }
  }
  await page.waitForTimeout(1500);
  await shot(page, '03-connector-picker');

  // Pick the network_dir connector card.
  for (const t of ['Files and Directories', 'Files & Directories', 'network_dir', 'Network']) {
    const c = page.getByText(t, { exact: false }).first();
    if (await c.count().catch(() => 0)) {
      try { await c.click({ timeout: 3000 }); console.log('clicked connector:', t); break; } catch (e) {}
    }
  }
  await page.waitForTimeout(1800);
  await shot(page, '04-networkdir-form');

  // Fill realistic values so the screenshot shows glob + index_mode in use.
  const fill = async (ph, val) => {
    const el = page.locator(`input[placeholder="${ph}"]`).first();
    if (await el.count()) { await el.fill(val); }
  };
  await fill('Directory Path', '/mnt/finance-share');
  await fill('Include Patterns (globs)', 'files/**/*.ppt, reports/**/*.csv, docs/**');
  // Select the Indexing (index_mode) dropdown → "content".
  const sel = page.locator('select').first();
  if (await sel.count()) { await sel.selectOption('content').catch(() => {}); }
  // Scroll the glob field into view within the modal, then capture.
  await page.locator('input[placeholder="Include Patterns (globs)"]').first().scrollIntoViewIfNeeded().catch(() => {});
  await page.waitForTimeout(600);
  await shot(page, '05-networkdir-filled');
  // Wheel-scroll the modal's inner content down to reveal the Indexing select.
  await page.mouse.move(720, 500);
  await page.mouse.wheel(0, 520);
  await page.waitForTimeout(700);
  await shot(page, '06-networkdir-indexing');

  // --- S3 form ---
  await page.goto(`${BASE}/agents/new`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  for (const t of ['New connection', 'Add connection', 'Connect']) {
    const b = page.getByText(t, { exact: false }).first();
    if (await b.count().catch(() => 0)) { try { await b.click({ timeout: 3000 }); break; } catch (e) {} }
  }
  await page.waitForTimeout(1200);
  for (const t of ['Amazon S3', 'S3']) {
    const c = page.getByText(t, { exact: false }).first();
    if (await c.count().catch(() => 0)) { try { await c.click({ timeout: 3000 }); break; } catch (e) {} }
  }
  await page.waitForTimeout(1800);
  await fill('Include Patterns (globs)', 'docs/**/*.pdf, reports/**/*.csv');
  await page.locator('input[placeholder="Include Patterns (globs)"]').first().scrollIntoViewIfNeeded().catch(() => {});
  await page.waitForTimeout(600);
  await shot(page, '07-s3-form');

  await browser.close();
  console.log('done');
};

run().catch((e) => { console.error('ERR', e); process.exit(1); });
