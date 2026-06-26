// Verifies agent conversation starters are sourced from the Prompt API (not the
// legacy data_source.conversation_starters JSON). Self-contained: signs in via
// the UI with a pre-seeded admin user, deep-links to the seeded agent, asserts
// the starter buttons render, opens the Edit modal, adds a starter, saves, and
// confirms persistence via GET /api/prompts (and that the legacy JSON is NOT
// written by the save).
import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Seeded by the harness (scratchpad/agent-starters.env.json). Falls back to env.
const envFile = path.join(__dirname, 'env.json');
const seeded = fs.existsSync(envFile) ? JSON.parse(fs.readFileSync(envFile, 'utf-8')) : {};
const EMAIL = process.env.STARTER_EMAIL || seeded.email;
const PASSWORD = process.env.STARTER_PASSWORD || seeded.password;
const DS_ID = process.env.STARTER_DS_ID || seeded.dsId;
const ORG_ID = process.env.STARTER_ORG_ID || seeded.orgId;
const SHOTS = path.resolve(__dirname, '../../../docs/design/screenshots');

async function signIn(page: any) {
  await page.goto('/users/sign-in', { waitUntil: 'load' });
  await page.waitForSelector('#email', { state: 'visible', timeout: 30000 });
  await page.fill('#email', EMAIL);
  await page.fill('#password', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL((url: URL) => !url.pathname.includes('/users/sign-in'), { timeout: 30000 });
}

test('agent page renders starter buttons from Prompts and modal edits persist via Prompt API', async ({ page }) => {
  expect(EMAIL && PASSWORD && DS_ID, 'seed env present').toBeTruthy();

  await signIn(page);

  // Deep-link straight to the seeded agent's detail view.
  await page.goto(`/agents/${DS_ID}`, { waitUntil: 'load' });

  // The two seeded starters render as clickable buttons (sourced from Prompts).
  const topStarter = page.getByRole('button', { name: 'Top customers by revenue' });
  const mauStarter = page.getByRole('button', { name: 'Monthly active users' });
  await expect(topStarter).toBeVisible({ timeout: 30000 });
  await expect(mauStarter).toBeVisible();

  await page.screenshot({ path: path.join(SHOTS, 'agent-starters.png'), fullPage: true });

  // Open the Edit conversation starters modal. Scope to the starters header row
  // so we click the right "Edit" (there are other Edit buttons on the page).
  const startersRow = page.locator('div', {
    has: page.getByText('Conversation starters', { exact: true }),
  }).filter({ hasText: 'Edit' }).last();
  await startersRow.getByRole('button', { name: 'Edit' }).click();
  await expect(page.getByText('Edit conversation starters')).toBeVisible({ timeout: 10000 });

  // Add a brand-new starter via the modal.
  const newTitle = 'Revenue by region';
  const newPrompt = 'Break down revenue by region for the current quarter.';
  await page.getByRole('button', { name: 'Add starter' }).click();
  const titleInputs = page.locator('input[placeholder="Title"]');
  const promptInputs = page.locator('textarea[placeholder="Prompt"]');
  const lastIdx = (await titleInputs.count()) - 1;
  await titleInputs.nth(lastIdx).fill(newTitle);
  await promptInputs.nth(lastIdx).fill(newPrompt);

  await page.screenshot({ path: path.join(SHOTS, 'agent-starters-modal.png'), fullPage: true });

  // Save (replace-all DELETE + POST against the Prompt API).
  await page.getByRole('button', { name: /^Save$/ }).click();
  await expect(page.getByText('Edit conversation starters')).toBeHidden({ timeout: 15000 });

  // The new starter button now appears (sourced from refetched Prompts).
  await expect(page.getByRole('button', { name: newTitle })).toBeVisible({ timeout: 15000 });

  // Persistence is via the Prompt API. Read the bearer token from the auth cookie
  // (falls back to the in-page session). The browser request context carries the
  // auth cookie automatically; we add the X-Organization-Id header explicitly.
  const cookies = await page.context().cookies();
  const authCookie = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token');
  const rawToken = authCookie ? decodeURIComponent(authCookie.value) : '';
  const authHeader = rawToken.startsWith('Bearer') ? rawToken : `Bearer ${rawToken}`;
  const headers = { Authorization: authHeader, 'X-Organization-Id': ORG_ID || '' };

  const apiResp = await page.request.get(
    `/api/prompts?data_source_id=${DS_ID}&starters_only=true`, { headers });
  expect(apiResp.ok()).toBeTruthy();
  const body = await apiResp.json();
  const titles = (body.prompts || []).map((p: any) => String(p.text || '').split('\n')[0]);
  expect(titles).toContain(newTitle);

  // The legacy data_source.conversation_starters JSON is NOT written by the save.
  const dsResp = await page.request.get(`/api/data_sources/${DS_ID}`, { headers });
  if (dsResp.ok()) {
    const ds = await dsResp.json();
    expect(ds.conversation_starters || []).not.toContain(`${newTitle}\n${newPrompt}`);
  }
});
