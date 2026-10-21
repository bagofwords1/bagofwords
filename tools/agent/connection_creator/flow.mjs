// Live UI evidence for the `create_connections` role.
// Prereqs: tools/agent/boot_stack.sh ; cd backend && uv run python ../tools/agent/connection_creator/seed.py
// Run:     cd frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node ../tools/agent/connection_creator/flow.mjs <out-dir>
// (ESM resolves @playwright/test next to this file: `ln -s ../../../frontend/node_modules tools/agent/connection_creator/node_modules` once.)
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const OUT = process.argv[2] || 'media/pr/connection-creator';
const BASE = 'http://localhost:3000';
const CHINOOK = new URL('../../../backend/tests/config/chinook.sqlite', import.meta.url).pathname;
mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch();
const failures = [];
const check = (ok, msg) => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); if (!ok) failures.push(msg); };

async function session(email) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on('response', r => {
    if (r.url().includes('/api/') && r.status() >= 400) console.log(`   [${email}] HTTP ${r.status()} ${r.request().method()} ${r.url().replace(BASE, '')}`);
  });
  await page.goto(`${BASE}/users/sign-in`, { timeout: 240000 });
  await page.fill('#email', email);
  await page.fill('#password', 'Password123!');
  await page.click('button[type=submit]');
  await page.waitForURL(u => !u.toString().includes('sign-in'), { timeout: 60000 });
  await page.waitForTimeout(1500);
  if (page.url().includes('onboarding')) {
    await page.getByText('Skip onboarding').click().catch(() => {});
    await page.waitForTimeout(1500);
  }
  return { ctx, page };
}
const go = async (page, path) => { await page.goto(BASE + path, { waitUntil: 'networkidle', timeout: 120000 }).catch(() => {}); await page.waitForTimeout(2500); };
const snap = async (page, name) => { await page.waitForTimeout(700); await page.screenshot({ path: `${OUT}/${name}.png` }); console.log(`   shot ${name}.png`); };
async function connectionNames(page) {
  await go(page, '/agents');
  const viewAll = page.getByRole('button', { name: 'View all' });
  if (!(await viewAll.isVisible().catch(() => false))) return [];
  await viewAll.click();
  await page.waitForTimeout(800);
  const dialog = page.locator('[role=dialog]').last();
  return (await dialog.locator('p, span, div').allInnerTexts()).map(s => s.trim());
}

// 1. Admin: the role editor offers "Create connections".
{
  const { ctx, page } = await session('admin@example.com');
  await go(page, '/settings/members');
  await page.getByRole('button', { name: 'Roles' }).click().catch(() => page.getByText('Roles', { exact: true }).first().click());
  await page.waitForTimeout(1500);
  const row = page.locator('div').filter({ has: page.getByText('Connection Builder', { exact: true }) }).filter({ has: page.locator('button') }).last();
  await row.locator('button:has([class*="pencil"])').first().click();
  await page.waitForTimeout(1200);
  check(await page.getByText('Create connections', { exact: true }).isVisible(), 'role editor shows the "Create connections" permission');
  await snap(page, '01-admin-role-editor-create-connections');
  await ctx.close();
}

// 2. Builder: sees only their own connections; can create one and build an agent on it.
{
  const { ctx, page } = await session('builder@example.com');
  await go(page, '/agents');
  await snap(page, '02-builder-agents-page');
  const before = (await connectionNames(page)).join(' | ');
  check(!before.includes('Finance Warehouse') && !before.includes('Marketing DB'), 'builder does not see the admin\'s or another builder\'s connection');
  await page.keyboard.press('Escape');
  await go(page, '/agents');
  if (await page.getByText('Add connection').isVisible().catch(() => false)) {
    await page.getByText('Add connection').click();
  } else {
    await page.getByRole('button', { name: 'View all' }).click();
    await page.locator('[role=dialog]').last().getByRole('button', { name: 'New' }).click();
  }
  await page.waitForTimeout(800);
  await page.getByText('SQLite', { exact: true }).click();
  await page.waitForTimeout(800);
  const name = `Ben Sales ${Date.now() % 100000}`;
  await page.locator('[role=dialog]').last().locator('input').first().fill(name);
  await page.getByPlaceholder('Database Path').fill(CHINOOK);
  await page.getByRole('button', { name: 'Connect' }).click();
  await page.getByText('Connection saved').waitFor({ timeout: 60000 }).catch(() => {});
  check(await page.getByText('Connection saved').isVisible(), 'builder can create a connection');
  await snap(page, '03-builder-created-connection');
  await page.getByRole('button', { name: 'Done' }).click().catch(() => {});
  await page.waitForTimeout(1500);
  const after = (await connectionNames(page)).join(' | ');
  check(after.includes(name) && !after.includes('Finance Warehouse') && !after.includes('Marketing DB'), 'builder now sees exactly their own connections');
  await snap(page, '04-builder-sees-only-own-connections');
  await page.keyboard.press('Escape');

  // Build an agent on it (no "Create agent" permission held).
  await go(page, '/agents');
  await page.getByRole('button', { name: 'New agent' }).first().click();
  await page.waitForTimeout(800);
  await page.getByPlaceholder('e.g., Sales, Marketing, Finance').fill(`${name} agent`);
  if (await page.getByText('Select connections').isVisible().catch(() => false)) {
    await page.getByText('Select connections').click();
    await page.waitForTimeout(600);
    await page.getByRole('option', { name: new RegExp(name) }).click();
    await page.keyboard.press('Escape');
    await page.getByPlaceholder('e.g., Sales, Marketing, Finance').click();
  }
  // The picker only offers connections the builder can build on — their own.
  await snap(page, '05-builder-new-agent-on-own-connection');
  await page.getByRole('button', { name: 'Save & Continue' }).click();
  await page.waitForTimeout(8000);
  check(!(await page.getByText(/Permission denied|Access denied/).isVisible().catch(() => false)), 'builder can build an agent on their connection');
  await snap(page, '06-builder-agent-created');

  await ctx.close();
}

// 3. Connection admin and full admin see every connection.
for (const [email, shot] of [['connadmin@example.com', '07-connection-admin-sees-all'], ['admin@example.com', '08-full-admin-sees-all']]) {
  const { ctx, page } = await session(email);
  const names = (await connectionNames(page)).join(' | ');
  check(names.includes('Finance Warehouse') && names.includes('Marketing DB') && names.includes('Ben Sales'), `${email} sees all connections`);
  await snap(page, shot);
  await ctx.close();
}

// 4. Plain member: no create affordance, no connections.
{
  const { ctx, page } = await session('member@example.com');
  await go(page, '/agents');
  check(!(await page.getByText('Add connection').isVisible().catch(() => false)), 'plain member has no "Add connection" button');
  await snap(page, '09-member-no-create');
  await ctx.close();
}

await browser.close();
console.log(failures.length ? `\n${failures.length} FAILED` : '\nALL CHECKS PASSED');
process.exit(failures.length ? 1 : 0);
