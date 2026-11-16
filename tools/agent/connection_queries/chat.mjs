// Start a chat the way a person does: open a report on an agent, type, send.
import { ORIGIN } from './lib.mjs';
export async function api(page, path, method = 'GET', body) {
  return page.evaluate(async ({ path, method, body }) => {
    const token = decodeURIComponent((document.cookie.match(/auth\.token=([^;]+)/) || [])[1] || '').replace(/^Bearer\s+/, '');
    const orgs = await (await fetch('/api/organizations', { headers: { Authorization: `Bearer ${token}` } })).json();
    const r = await fetch(path, { method, headers: { Authorization: `Bearer ${token}`, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    return { status: r.status, json: await r.json().catch(() => null) };
  }, { path, method, body });
}
export async function startChat(page, { title, dsId, prompt }) {
  if (!page.url().startsWith(ORIGIN)) await page.goto(`${ORIGIN}/reports`, { waitUntil: 'domcontentloaded' });
  const created = await api(page, '/api/reports', 'POST', { title, data_sources: [dsId] });
  if (created.status >= 300) throw new Error(`report: ${created.status} ${JSON.stringify(created.json)}`);
  const id = created.json.id;
  await page.goto(`${ORIGIN}/reports/${id}`, { waitUntil: 'domcontentloaded' });
  const input = page.locator('[contenteditable="true"]').first();
  await input.waitFor({ timeout: 120000 });
  await input.click();
  await page.keyboard.type(prompt, { delay: 2 });
  await page.waitForTimeout(400);
  await page.locator('button[class*="rounded-full"]').last().click();
  return id;
}
