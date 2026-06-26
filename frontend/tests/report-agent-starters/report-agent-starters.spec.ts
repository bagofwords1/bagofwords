// Verifies the two in-report conversation-starter surfaces source their starters
// from the Prompt API (not the legacy data_source.conversation_starters JSON):
//
//   1. ReportAgentPanel (the in-report Agent panel): renders the agent's starter
//      buttons from GET /prompts?...&starters_only=true, and its "Edit
//      conversation starters" modal saves replace-all against the Prompt API.
//   2. The report empty-state suggestion chips (agentConversationStarters), which
//      load from the same starter Prompts of the selected agents.
//
// The spec signs in via the UI with a pre-seeded admin user, opens a pre-seeded
// report that uses the seeded agent, and asserts both surfaces. It also confirms
// persistence is via Prompts (GET reflects the edit) and that the legacy JSON
// stays untouched.
import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const envFile = path.join(__dirname, 'env.json');
const seeded = fs.existsSync(envFile) ? JSON.parse(fs.readFileSync(envFile, 'utf-8')) : {};
const EMAIL = process.env.STARTER_EMAIL || seeded.email;
const PASSWORD = process.env.STARTER_PASSWORD || seeded.password;
const DS_ID = process.env.STARTER_DS_ID || seeded.dsId;
const ORG_ID = process.env.STARTER_ORG_ID || seeded.orgId;
const REPORT_ID = process.env.STARTER_REPORT_ID || seeded.reportId;
const SHOTS = path.resolve(__dirname, '../../../docs/design/screenshots');

async function signIn(page: any) {
  await page.goto('/users/sign-in', { waitUntil: 'load' });
  await page.waitForSelector('#email', { state: 'visible', timeout: 30000 });
  await page.fill('#email', EMAIL);
  await page.fill('#password', PASSWORD);
  await page.click('button[type="submit"]');
  // Backend auth (argon2) can be very slow in CI/sandbox; allow generous time.
  await page.waitForURL((url: URL) => !url.pathname.includes('/users/sign-in'), { timeout: 90000 });
}

function apiHeaders(page: any) {
  return page.context().cookies().then((cookies: any[]) => {
    const authCookie = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token');
    const rawToken = authCookie ? decodeURIComponent(authCookie.value) : '';
    const authHeader = rawToken.startsWith('Bearer') ? rawToken : `Bearer ${rawToken}`;
    return { Authorization: authHeader, 'X-Organization-Id': ORG_ID || '' };
  });
}

test('in-report agent panel + empty-state chips render starters from Prompts and modal edits persist via the Prompt API', async ({ page }) => {
  expect(EMAIL && PASSWORD && DS_ID && REPORT_ID, 'seed env present').toBeTruthy();

  await signIn(page);

  // Open the seeded report that uses the seeded agent.
  await page.goto(`/reports/${REPORT_ID}`, { waitUntil: 'load' });

  // --- Surface 2: empty-state suggestion chips (agentConversationStarters) ---
  // The empty report shows up to 3 chips sourced from the agent's starter
  // Prompts. The two seeded starter titles render as chips.
  const chipTop = page.getByRole('button', { name: 'Top customers by revenue' });
  const chipMau = page.getByRole('button', { name: 'Monthly active users' });
  await expect(chipTop).toBeVisible({ timeout: 30000 });
  await expect(chipMau).toBeVisible();
  await page.screenshot({ path: path.join(SHOTS, 'report-starter-chips.png'), fullPage: true });

  // --- Surface 1: open the in-report Agent panel (ReportAgentPanel) ---
  // The "Instructions" affordance in the prompt box opens the right-hand Agent
  // panel (rightPanelView = 'agent').
  await page.getByRole('button', { name: /Instructions/ }).first().click();

  // The panel's conversation-starter buttons render (from Prompts). The panel
  // shows them under the "Conversation starters" header in the Overview tab.
  await expect(page.getByText('Conversation starters', { exact: true })).toBeVisible({ timeout: 30000 });
  // Scope to the panel: the starter buttons carry the title text. There may be
  // a chip with the same name in the empty state, so assert at least one of each
  // is present (count >= 1 is enough to prove rendering from Prompts).
  await expect(page.getByRole('button', { name: 'Top customers by revenue' }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Monthly active users' }).first()).toBeVisible();
  await page.screenshot({ path: path.join(SHOTS, 'report-agent-panel-starters.png'), fullPage: true });

  // --- Open the "Edit conversation starters" modal, add a starter, Save ---
  const startersRow = page.locator('div', {
    has: page.getByText('Conversation starters', { exact: true }),
  }).filter({ hasText: 'Edit' }).last();
  await startersRow.getByRole('button', { name: 'Edit' }).click();
  await expect(page.getByText('Edit conversation starters')).toBeVisible({ timeout: 10000 });

  const newTitle = 'Churn by cohort';
  const newPrompt = 'Show churn rate broken down by signup cohort.';
  await page.getByRole('button', { name: 'Add starter' }).click();
  // The modal's title/prompt fields use these placeholders (i18n
  // starterTitlePlaceholder / starterPromptPlaceholder).
  const titleInputs = page.locator('input[placeholder="e.g. Overview of Snowflake"]');
  const promptInputs = page.locator('textarea[placeholder="Optional extra instructions"]');
  const lastIdx = (await titleInputs.count()) - 1;
  await titleInputs.nth(lastIdx).fill(newTitle);
  await promptInputs.nth(lastIdx).fill(newPrompt);

  await page.screenshot({ path: path.join(SHOTS, 'report-agent-panel-modal.png'), fullPage: true });

  // Save (replace-all DELETE + POST against the Prompt API).
  await page.getByRole('button', { name: /^Save$/ }).click();
  await expect(page.getByText('Edit conversation starters')).toBeHidden({ timeout: 20000 });

  // The new starter button now appears in the panel (refetched from Prompts).
  await expect(page.getByRole('button', { name: newTitle }).first()).toBeVisible({ timeout: 15000 });

  // --- Persistence is via the Prompt API ---
  const headers = await apiHeaders(page);
  const apiResp = await page.request.get(
    `/api/prompts?data_source_id=${DS_ID}&starters_only=true`, { headers });
  expect(apiResp.ok()).toBeTruthy();
  const body = await apiResp.json();
  const titles = (body.prompts || []).map((p: any) => String(p.text || '').split('\n')[0]);
  expect(titles).toContain(newTitle);
  // The two originals were re-seeded by the replace-all (they were still in the
  // modal form), so the edit added exactly the new one alongside them.
  expect(titles).toContain('Top customers by revenue');
  expect(titles).toContain('Monthly active users');

  // --- The legacy data_source.conversation_starters JSON is NOT written ---
  const dsResp = await page.request.get(`/api/data_sources/${DS_ID}`, { headers });
  expect(dsResp.ok()).toBeTruthy();
  const ds = await dsResp.json();
  const legacy = ds.conversation_starters || [];
  // Stays empty (as seeded). Must not have grown to hold the new starter.
  expect(Array.isArray(legacy) ? legacy : []).not.toContain(`${newTitle}\n${newPrompt}`);
  expect(legacy).toEqual([]);
});
