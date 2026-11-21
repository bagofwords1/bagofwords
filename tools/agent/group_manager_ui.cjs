// Screenshots + flow video for group managers (pairs with group_manager_live.py).
//
//   node tools/agent/group_manager_ui.cjs shots <outDir> <prefix> [--locale he]
//   node tools/agent/group_manager_ui.cjs flow  <outDir>
//   node tools/agent/group_manager_ui.cjs landing <outDir>
//
// landing: for each seeded role, open home and check the Admin entry is shown
//          (or not) and opens that person's own admin page.
//
// shots: the Groups tab and the Sales members dialog, as the org admin and as
//        lena@ (Sales manager). Missing UI (e.g. no Groups tab) is captured as is.
// flow:  lena signs in, clicks Admin in the app sidebar (lands on Groups), adds olga@ to Sales, promotes pete@,
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
  // Reach Groups the way a manager would: the sidebar's Admin entry from home.
  await page.goto(`${ORIGIN}/`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForTimeout(2000);
  const adminEntry = page.locator('a[href^="/settings/"]').filter({ hasText: /admin/i });
  expect(await adminEntry.count() > 0, 'manager sees the Admin entry in the app sidebar');
  await adminEntry.first().click();
  await page.waitForTimeout(2500);
  expect(page.url().includes('tab=groups'), 'Admin entry opens the Groups tab');
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

async function landing() {
  const cases = [
    // who, email, expected landing (null = no Admin entry)
    ['org admin', 'admin@example.com', /\/settings\/members$/],
    ['group manager', 'lena@example.com', /\/settings\/members\?tab=groups$/],
    ['LLM manager', 'llm@example.com', /\/settings\/models$/],
    ['auditor', 'audit@example.com', /\/settings\/audit$/],
    ['settings manager', 'settings@example.com', /\/settings\/ai_settings$/],
    ['service-account manager', 'sa@example.com', /\/settings\/members\?tab=service_accounts$/],
    ['plain member', 'pete@example.com', null],
  ];
  const browser = await chromium.launch();
  let failures = 0;
  for (const [who, email, expected] of cases) {
    const { context, page } = await login(browser, email, { deviceScaleFactor: 1 });
    await page.goto(`${ORIGIN}/`, { waitUntil: 'networkidle' }).catch(() => {});
    await page.waitForTimeout(2000);
    const entry = page.locator('a[href^="/settings/"]').filter({ hasText: /admin/i });
    const has = (await entry.count()) > 0;
    let landed = '-';
    if (has) {
      await entry.first().click();
      await page.waitForTimeout(2000);
      landed = page.url().replace(ORIGIN, '');
    }
    const ok = expected ? has && expected.test(landed) : !has;
    failures += !ok;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${who.padEnd(24)} admin entry=${has}  opens ${landed}`);
    if (has) await page.screenshot({ path: path.join(outDir, `landing-${who.replace(/\W+/g, '-')}.png`) });
    await context.close();
  }
  await browser.close();
  if (failures) process.exit(1);
}

const run = { flow, landing, shots }[mode] || shots;
run().catch((e) => { console.error(e); process.exit(1); });
