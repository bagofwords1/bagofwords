// Agents-page "Push to Git" loop. Run from frontend/: node <this file> <before|after|member|disabled> <outDir>
// Resolve playwright from the cwd (frontend/), not from docs/.
import { createRequire } from 'node:module';
const { chromium } = createRequire(`${process.cwd()}/package.json`)('playwright');
const [mode, outDir] = process.argv.slice(2); // mode: before | after | member | disabled | he
const origin = 'http://localhost:3000';
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
const email = mode === 'member' ? 'member@example.com' : 'admin@example.com';
await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForSelector('#email', { timeout: 120000 }); await page.fill('#email', email); await page.fill('#password', 'Password123!');
await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}), page.click('button[type=submit]')]);
await page.waitForTimeout(1500);
if (page.url().includes('/onboarding')) { try { await page.getByText('Skip onboarding').click({ timeout: 4000 }); await page.waitForTimeout(1500) } catch {} }
if (mode === 'he') await page.evaluate(() => localStorage.setItem('bow.locale', 'he'));
await page.goto(`${origin}/agents`, { waitUntil: 'load', timeout: 180000 });
await page.getByRole('heading', { level: 1 }).waitFor({ timeout: 120000 });
await page.waitForTimeout(2500);
const btn = page.getByTestId('agents-push-to-git');
const header = { x: 0, y: 0, width: 1440, height: 110 };
console.log(`[${mode}] push button count:`, await btn.count());
await page.screenshot({ path: `${outDir}/${mode}-header.png`, clip: header });
if (mode === 'disabled') {
  console.log('[disabled] isDisabled:', await btn.isDisabled(), 'title:', await btn.getAttribute('title'));
}
if (mode === 'after') {
  console.log('[after] title:', await btn.getAttribute('title'));
  await btn.click();
  const dialog = page.getByRole('dialog');
  const inModal = dialog.getByRole('button', { name: 'Push to Git' });
  await inModal.waitFor({ timeout: 120000 });
  console.log('[after] modal push button visible:', await inModal.isVisible());
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${outDir}/after-builds-modal.png` });
  const pushReq = page.waitForRequest(r => /\/api\/git\/[^/]+\/push$/.test(r.url()) && r.method() === 'POST', { timeout: 60000 });
  await inModal.click();
  const req = await pushReq;
  console.log('[after] POST', new URL(req.url()).pathname, req.postData());
  const resp = await req.response();
  console.log('[after] response', resp.status(), (await resp.text()).slice(0, 300));
  await page.getByText('Pushed to Git').first().waitFor({ timeout: 15000 });
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${outDir}/after-pushed.png` });
}
await browser.close();
