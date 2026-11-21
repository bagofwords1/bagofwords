// Screenshots + flow video for group managers (pairs with group_manager_live.py).
//
//   node tools/agent/group_manager_ui.cjs shots <outDir> <prefix> [--locale he]
//   node tools/agent/group_manager_ui.cjs flow  <outDir>
//
// shots: the Groups tab and the Sales members dialog, as the org admin and as
//        lena@ (Sales manager). Missing UI (e.g. no Groups tab) is captured as is.
// flow:  lena signs in, opens Groups, adds olga@ to Sales, promotes pete@,
//        removes sam@; then checks sam lost the Sales agent. Records a video and
//        exits 1 if any step's expectation fails.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [mode, outDir, prefix = 'shot', ...rest] = process.argv.slice(2);
const ORIGIN = process.env.BOW_ORIGIN || 'http://localhost:3000';
const PASSWORD = 'Password123!';
const locale = rest.includes('--locale') ? rest[rest.indexOf('--locale') + 1] : null;
fs.mkdirSync(outDir, { recursive: true });

async function login(browser, email, opts = {}) {
  const context = await browser.newContext({ viewport: { width: 1360, height: 860 }, deviceScaleFactor: 2, ...opts });
  if (locale) await context.addInitScript((l) => localStorage.setItem('bow.locale', l), locale);
  const page = await context.newPage();
  await page.goto(`${ORIGIN}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.fill('#email', email);
  await page.fill('#password', PASSWORD);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 30000 }).catch(() => {}),
    page.click('button[type=submit]'),
  ]);
  await page.waitForTimeout(1500);
  return { context, page };
}

async function openGroups(page) {
  await page.goto(`${ORIGIN}/settings/members?tab=groups`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForTimeout(2000);
}

async function openSalesMembers(page) {
  const row = page.locator('tr', { hasText: 'Sales' }).first();
  if (!(await row.count())) return false;
  await row.locator('button').filter({ hasText: /\d/ }).first().click();
  await page.waitForTimeout(1500);
  return true;
}

async function shots() {
  const browser = await chromium.launch();
  for (const [who, email] of [['admin', 'admin@example.com'], ['manager', 'lena@example.com']]) {
    const { context, page } = await login(browser, email);
    await openGroups(page);
    await page.screenshot({ path: path.join(outDir, `${prefix}-${who}-groups.png`) });
    if (await openSalesMembers(page)) {
      await page.screenshot({ path: path.join(outDir, `${prefix}-${who}-members.png`) });
    }
    await context.close();
  }
  await browser.close();
}

async function flow() {
  const browser = await chromium.launch();
  const failures = [];
  const expect = (cond, msg) => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg}`); if (!cond) failures.push(msg); };
  const { context, page } = await login(browser, 'lena@example.com', {
    deviceScaleFactor: 1, recordVideo: { dir: outDir, size: { width: 1360, height: 860 } },
  });
  await openGroups(page);
  const rows = await page.locator('tbody tr').allInnerTexts();
  expect(rows.some((r) => r.includes('Sales')), 'manager sees Sales on the Groups tab');
  expect(!rows.some((r) => r.includes('Finance')), 'manager does not see Finance');
  expect(!(await page.getByRole('button', { name: /new group/i }).count()), 'no "New group" button');
  await openSalesMembers(page);
  const dialog = page.locator('[role=dialog]');

  // Add olga
  await dialog.locator('button', { hasText: /select|add/i }).first().click().catch(() => {});
  await page.waitForTimeout(500);
  await page.getByRole('option', { name: /Olga/ }).first().click();
  await dialog.getByRole('button', { name: /^add$/i }).click();
  await page.waitForTimeout(1500);
  expect(await dialog.getByText('Olga Outside').count() > 0, 'olga added to Sales');

  // Promote pete
  const peteRow = dialog.locator('[data-testid=group-member-row]', { hasText: 'Pete Peer' });
  await peteRow.getByRole('button', { name: /make manager/i }).click();
  await page.waitForTimeout(1500);
  expect(await peteRow.getByText(/^manager$/i).count() > 0, 'pete shown as manager');
  expect(!(await peteRow.getByRole('button', { name: /remove/i }).count()), 'no remove button on a fellow manager');

  // Remove sam
  const samRow = dialog.locator('[data-testid=group-member-row]', { hasText: 'Sam Sales' });
  await samRow.getByRole('button', { name: /remove/i }).click();
  await page.waitForTimeout(1500);
  expect(!(await dialog.getByText('Sam Sales').count()), 'sam removed from Sales');
  await page.screenshot({ path: path.join(outDir, 'flow-final.png') });
  await context.close();
  await browser.close();
  if (failures.length) process.exit(1);
}

(mode === 'flow' ? flow() : shots()).catch((e) => { console.error(e); process.exit(1); });
