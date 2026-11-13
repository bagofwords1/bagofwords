// Real-stack regression and UI evidence. See docs/feedback-loops/agent-member-access.md.
import { chromium, expect } from '@playwright/test';
import { readFileSync, mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
const base = process.env.BASE_URL || 'http://localhost:3107';
const seed = JSON.parse(readFileSync(process.env.BOW_ACCESS_SEED, 'utf8'));
const out = resolve('../media/pr/agent-member-access');
mkdirSync(out, { recursive: true });
const agentId = seed.sqlite_sources[0].id;
const orgId = seed.organization.id;
const api = seed.base_url;
const headers = { Authorization: `Bearer ${seed.admin.token}`, 'X-Organization-Id': orgId };
async function request(path, method = 'GET', body, auth = headers) {
  const response = await fetch(api + '/api' + path, { method, headers: { ...auth, 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) throw Error(`${method} ${path}: ${response.status} ${await response.text()}`);
  return response.status === 204 ? null : response.json();
}
const users = await request('/organization/members');
const analyst = users.find(u => u.email === 'analyst@example.com');
const manager = users.find(u => u.email === 'manager@example.com');
const reader = users.find(u => u.email === 'reader@example.com');
expect(analyst && manager && reader, 'Seed analyst, manager and reader first').toBeTruthy();
const memberships = () => request(`/data_sources/${agentId}/members`);
async function grant(user, permissions) {
  let member = (await memberships()).find(m => m.principal_id === user.id && m.principal_type === 'user');
  if (!member) {
    await request(`/data_sources/${agentId}/members`, 'POST', { principal_type: 'user', principal_id: user.id });
    member = (await memberships()).find(m => m.principal_id === user.id);
  }
  await request(`/organizations/${orgId}/resource-grants/${member.id}`, 'PUT', { permissions });
  return member.id;
}
await grant(analyst, []);
const managerGrant = await grant(manager, ['manage']);
let readerMember = (await memberships()).find(m => m.principal_id === reader.id);
if (readerMember) await request(`/data_sources/${agentId}/members/${reader.id}`, 'DELETE');
await request(`/data_sources/${agentId}`, 'PUT', { is_public: true });
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, recordVideo: { dir: '/tmp/bow-agent-access-video', size: { width: 1280, height: 1000 } } });
await context.addCookies([{ name: 'auth.token', value: seed.admin.token, url: base }]);
const page = await context.newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));
const url = `${base}/agents/${agentId}/settings`;
const row = email => page.getByTestId('agent-member-row').filter({ hasText: email });
const edit = email => row(email).getByRole('button', { name: /^Edit access for/ });
const dialog = page.getByRole('dialog');
const screenshot = name => page.screenshot({ path: `${out}/${name}.png`, animations: 'disabled' });
try {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 120000 });
  // The reported regression: public access used to remove all member controls.
  await expect(page.getByRole('button', { name: 'Add member', exact: true })).toBeVisible({ timeout: 45000 });
  await expect(page.getByTestId('agent-public-access')).toHaveText(/Everyone in the organization.*Can query/);
  await expect(row('admin@example.com')).toContainText('Can manage');
  await expect(edit('admin@example.com')).toHaveCount(0);
  await expect(edit('analyst@example.com')).toHaveText('Can query');
  await screenshot('after-public');

  await edit('analyst@example.com').click();
  await expect(dialog.getByRole('checkbox')).toHaveCount(0);
  await dialog.getByRole('radio', { name: /Can manage/ }).click();
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeHidden();
  await expect(edit('analyst@example.com')).toHaveText('Can manage');
  expect((await memberships()).find(m => m.principal_id === analyst.id).permissions).toEqual(['manage']);
  console.log('PASS: public agent query → manage persists through the real API');

  await edit('analyst@example.com').click();
  await dialog.getByRole('radio', { name: /Can query/ }).click();
  await dialog.getByRole('button', { name: 'Advanced permissions' }).click();
  await dialog.getByRole('checkbox', { name: 'Manage instructions', exact: true }).check();
  await screenshot('after-advanced');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeHidden();
  await expect(edit('analyst@example.com')).toHaveText('Custom access');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(edit('analyst@example.com')).toHaveText('Custom access');
  await edit('analyst@example.com').click();
  await expect(dialog.getByRole('checkbox')).toHaveCount(0);
  await dialog.getByRole('button', { name: 'Advanced permissions' }).click();
  await expect(dialog.getByRole('checkbox', { name: 'Manage instructions', exact: true })).toBeChecked();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  console.log('PASS: custom permissions survive save/reload and Advanced starts collapsed');

  // A failed edit must retain both the saved role and the open editor.
  await edit('analyst@example.com').click();
  await dialog.getByRole('radio', { name: /Can manage/ }).click();
  await page.route('**/resource-grants/*', route => route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"Simulated save failure"}' }));
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('Failed to update permissions', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Save', exact: true })).toBeVisible();
  expect((await memberships()).find(m => m.principal_id === analyst.id).permissions).toEqual(['manage_instructions']);
  await page.unroute('**/resource-grants/*');
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(edit('analyst@example.com')).toHaveText('Custom access');
  console.log('PASS: failed save preserves the existing role');

  // Add uses the same two tiers and Advanced controls.
  await page.getByRole('button', { name: 'Add member', exact: true }).click();
  await expect(dialog.getByRole('radio', { name: /Can query/ })).toHaveAttribute('aria-checked', 'true');
  await expect(dialog.getByRole('checkbox')).toHaveCount(0);
  await dialog.locator('[aria-haspopup="listbox"]').click();
  await page.getByRole('option').filter({ hasText: 'reader@example.com' }).click();
  await page.keyboard.press('Escape');
  await dialog.getByRole('radio', { name: /Can manage/ }).click();
  await screenshot('after-add');
  await dialog.getByRole('button', { name: 'Add 1', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeHidden();
  await expect(edit('reader@example.com')).toHaveText('Can manage');
  expect((await memberships()).find(m => m.principal_id === reader.id).permissions).toEqual(['manage']);
  console.log('PASS: add manager on a public agent');

  // A membership may succeed while its second permission request fails.
  await request(`/data_sources/${agentId}/members/${reader.id}`, 'DELETE');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByRole('button', { name: 'Add member', exact: true }).click();
  await dialog.locator('[aria-haspopup="listbox"]').click();
  await page.getByRole('option').filter({ hasText: 'reader@example.com' }).click();
  await page.keyboard.press('Escape');
  await dialog.getByRole('radio', { name: /Can manage/ }).click();
  await page.route('**/resource-grants/*', route => route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"Simulated save failure"}' }));
  await dialog.getByRole('button', { name: 'Add 1', exact: true }).click();
  await expect(page.getByText('Some memberships or permissions could not be saved. Review the member list and retry.', { exact: true })).toBeVisible();
  expect((await memberships()).find(m => m.principal_id === reader.id).permissions).toEqual([]);
  await page.unroute('**/resource-grants/*');
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(edit('reader@example.com')).toHaveText('Can query');
  await edit('reader@example.com').click();
  await dialog.getByRole('radio', { name: /Can manage/ }).click();
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeHidden();
  await expect(edit('reader@example.com')).toHaveText('Can manage');
  console.log('PASS: partial add failure reports incomplete access and can be corrected');

  // Visibility toggles retain explicit grants.
  await request(`/data_sources/${agentId}`, 'PUT', { is_public: false });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(edit('analyst@example.com')).toHaveText('Custom access');
  await expect(page.getByTestId('agent-public-access')).toHaveCount(0);
  await screenshot('after-private');
  await request(`/data_sources/${agentId}`, 'PUT', { is_public: true });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(edit('reader@example.com')).toHaveText('Can manage');
  await row('reader@example.com').getByRole('button', { name: 'Remove', exact: true }).click();
  await expect(row('reader@example.com')).toHaveCount(0);
  expect((await memberships()).some(m => m.principal_id === reader.id)).toBe(false);
  console.log('PASS: public/private retains grants; removal deletes explicit access');

  // Hebrew RTL + narrow viewport evidence.
  await page.evaluate(() => localStorage.setItem('bow.locale', 'he'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  await expect(page.getByRole('button', { name: 'הוספת חבר', exact: true })).toBeVisible();
  await screenshot('after-he');
  await row('analyst@example.com').getByRole('button', { name: /^עריכת הגישה/ }).click();
  await expect(dialog.getByRole('button', { name: 'הרשאות מתקדמות' })).toBeVisible();
  await screenshot('after-he-editor');
  await page.setViewportSize({ width: 390, height: 844 });
  await screenshot('after-mobile-he-editor');
  await page.setViewportSize({ width: 1280, height: 1000 });
  await dialog.getByRole('button', { name: 'ביטול', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'ביטול', exact: true })).toBeHidden();
  console.log('PASS: Hebrew RTL access list and editor');

  // Resource-scoped managers have the same controls without org-admin status.
  const managerLogin = await fetch(api + '/api/auth/jwt/login', { method: 'POST', body: new URLSearchParams({ username: 'manager@example.com', password: seed.admin.password }) });
  const managerToken = (await managerLogin.json()).access_token;
  const managerContext = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
  await managerContext.addCookies([{ name: 'auth.token', value: managerToken, url: base }]);
  const managerPage = await managerContext.newPage();
  await managerPage.goto(url, { waitUntil: 'domcontentloaded' });
  await expect(managerPage.getByRole('button', { name: 'Add member', exact: true })).toBeVisible({ timeout: 30000 });
  await managerPage.getByTestId('agent-member-row').filter({ hasText: 'analyst@example.com' }).getByRole('button', { name: /^Edit access for/ }).click();
  await managerPage.getByRole('dialog').getByRole('radio', { name: /Can query/ }).click();
  await managerPage.getByRole('dialog').getByRole('button', { name: 'Save', exact: true }).click();
  await expect(managerPage.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true })).toBeHidden();
  expect((await memberships()).find(m => m.principal_id === analyst.id).permissions).toEqual([]);
  await managerContext.close();
  console.log('PASS: non-admin agent manager can edit public-agent permissions');

  // A plain public reader must be able to query/read, but not mutate grants.
  const login = await fetch(api + '/api/auth/jwt/login', { method: 'POST', body: new URLSearchParams({ username: 'reader@example.com', password: seed.admin.password }) });
  const readerAuth = { Authorization: `Bearer ${(await login.json()).access_token}`, 'X-Organization-Id': orgId };
  await request(`/data_sources/${agentId}`, 'GET', undefined, readerAuth);
  const denied = await fetch(api + `/api/organizations/${orgId}/resource-grants/${managerGrant}`, { method: 'PUT', headers: { ...readerAuth, 'Content-Type': 'application/json' }, body: JSON.stringify({ permissions: ['manage'] }) });
  expect(denied.status).toBe(403);
  const readerContext = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
  await readerContext.addCookies([{ name: 'auth.token', value: readerAuth.Authorization.slice(7), url: base }]);
  const readerPage = await readerContext.newPage();
  await readerPage.goto(url, { waitUntil: 'domcontentloaded' });
  await expect(readerPage.getByTestId('agent-public-access')).toBeVisible({ timeout: 30000 });
  await expect(readerPage.getByRole('button', { name: 'Add member', exact: true })).toHaveCount(0);
  await expect(readerPage.getByRole('button', { name: /^Edit access for/ })).toHaveCount(0);
  await readerPage.screenshot({ path: `${out}/after-reader.png` });
  await readerContext.close();
  console.log('PASS: public reader sees roles but has no management controls; API returns 403');
  expect(errors).toEqual([]);
} catch (error) {
  await screenshot('debug-failure');
  console.log('Browser errors:', errors);
  console.log((await page.locator('body').innerText()).slice(-1800));
  throw error;
} finally {
  await context.close();
  await browser.close();
}
