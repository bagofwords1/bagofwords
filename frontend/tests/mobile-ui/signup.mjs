// Creates an admin user + org via the real signup flow, saves storage state.
import { chromium } from '@playwright/test';
import fs from 'fs';

const BASE = process.env.BASE || 'http://localhost:3000';
const OUT = process.env.OUT || 'tests/mobile-ui/output';
fs.mkdirSync(OUT, { recursive: true });

const USER = {
  name: 'Avi Haimson',
  email: `mobileui-${Date.now()}@example.com`,
  password: 'TestPass123!',
};

const EXE = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const browser = await chromium.launch({ headless: true, executablePath: EXE });
const page = await browser.newPage();
await page.goto(`${BASE}/users/sign-up`, { waitUntil: 'load' });
await page.waitForSelector('#name', { state: 'visible', timeout: 30000 });
await page.fill('#name', USER.name);
await page.fill('#email', USER.email);
await page.fill('#password', USER.password);
await page.click('button[type="submit"]');
await Promise.race([
  page.waitForURL((u) => !u.pathname.includes('/users/sign-up'), { timeout: 20000 }).then(() => 'nav'),
  page.waitForSelector('.text-red-500', { timeout: 20000 }).then(() => 'err'),
]);
if (page.url().includes('/users/sign-up')) {
  const err = await page.locator('.text-red-500').textContent().catch(() => '');
  throw new Error('signup failed: ' + err);
}
await page.waitForLoadState('domcontentloaded');
await page.context().storageState({ path: `${OUT}/state.json` });
fs.writeFileSync(`${OUT}/user.json`, JSON.stringify(USER, null, 2));
console.log('SIGNUP_OK', USER.email, '->', page.url());
await browser.close();
