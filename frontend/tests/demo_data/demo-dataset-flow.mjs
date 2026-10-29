// Demo-data UI loop: drives the REAL stack (backend + frontend) through the
// create_demo_dataset review card and captures evidence.
//
// Prereqs (see docs/feedback-loops/demo-data-generation.md):
//   tools/agent/boot_stack.sh --dev
//   cd backend && uv run python ../tools/agent/seed_org.py
//   LLM: the scripted mock (tools/agent/mock_llm_demo_data.py) or a real
//   provider set as default + small default; org settings
//   enable_training_mode + enable_demo_data_generation on.
//
//   cd frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
//     BOW_TOKEN=<admin token> BOW_ORG=<org id> node tests/demo_data/demo-dataset-flow.mjs
import { chromium, expect } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const BASE = process.env.BOW_BASE || 'http://localhost:3000';
const API = process.env.BOW_API || 'http://localhost:8000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const TOKEN = process.env.BOW_TOKEN;
const ORG = process.env.BOW_ORG;
const PROMPT = process.env.BOW_PROMPT || 'I want a demo for finance in the e-commerce sector. Build the data and suggest agents.';
const out = fileURLToPath(new URL('../../../media/pr/demo-data-generation/', import.meta.url));
mkdirSync(out, { recursive: true });

async function newTrainingReport(title) {
  const r = await fetch(`${API}/api/reports`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${TOKEN}`, 'X-Organization-Id': ORG, 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, mode: 'training', data_sources: [] }),
  });
  if (!r.ok) throw new Error(`create report ${r.status}`);
  return (await r.json()).id;
}

async function sendPrompt(page, text) {
  const box = page.locator('[contenteditable="true"]').last();
  await box.waitFor({ timeout: 240000 });
  await page.waitForTimeout(1500);
  await box.click();
  await box.fill(text);
  await page.keyboard.press('Enter');
}

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1360, height: 900 }, recordVideo: { dir: out + 'video', size: { width: 1360, height: 900 } } });
const page = await context.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
try {
  // Login
  for (let i = 0; i < 4; i++) {  // dev server compiles pages on first hit
    await page.goto(`${BASE}/users/sign-in`);
    try { await page.locator('#email').waitFor({ timeout: 60000 }); break; } catch (e) { if (i === 3) throw e; }
  }
  await page.locator('#email').fill(EMAIL);
  await page.locator('#password').fill(PASSWORD);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 240000 });

  // Setting lives under Training mode in AI settings.
  await page.goto(`${BASE}/settings/ai_settings`);
  await page.getByText('Agent capabilities', { exact: true }).click({ timeout: 180000 });
  const toggleRow = page.getByText('Demo data generation', { exact: true });
  await expect(toggleRow).toBeVisible({ timeout: 180000 });
  await toggleRow.scrollIntoViewIfNeeded();
  await page.waitForTimeout(400);
  await page.screenshot({ path: out + '01_setting.png' });

  // Approve flow
  const reportId = await newTrainingReport('Demo data – finance');
  await page.goto(`${BASE}/reports/${reportId}`);
  await page.waitForTimeout(2500);
  await sendPrompt(page, PROMPT);

  const card = page.getByTestId('demo-dataset-card').last();
  const review = page.getByTestId('demo-dataset-review').last();
  await expect(review).toBeVisible({ timeout: 120000 });
  await card.locator('button:has(span.font-mono)', { hasText: 'orders' }).first().click(); // expand columns
  await page.waitForTimeout(300);
  await card.screenshot({ path: out + '02_review_card.png' });
  await page.screenshot({ path: out + '02_review_page.png' });

  // Untick the second suggested agent; the approve label follows.
  const agents = card.locator('[data-testid^="demo-agent-"]');
  const n = await agents.count();
  expect(n).toBeGreaterThan(0);
  if (n > 1) await agents.nth(1).click();
  await page.waitForTimeout(200);
  await card.screenshot({ path: out + '03_review_unticked.png' });

  await page.getByTestId('demo-dataset-approve').last().click();
  // Progress: spinner + per-table states while the small model generates.
  const tool = page.getByTestId('create-demo-dataset-tool').last();
  await expect(tool.getByText(/Generating data/)).toBeVisible({ timeout: 30000 });
  await page.waitForTimeout(1200);
  await tool.screenshot({ path: out + '04_generating.png' });

  await expect(page.getByText('Demo dataset created').last()).toBeVisible({ timeout: 240000 });
  await page.waitForTimeout(1500);
  await card.screenshot({ path: out + '05_created_card.png' });
  await page.screenshot({ path: out + '05_created_page.png' });
  // Only the ticked agent was created and it links to its page.
  await expect(card.getByRole('link', { name: /Open/ })).toHaveCount(n > 1 ? 1 : n);

  // Hebrew (RTL)
  await page.evaluate(() => localStorage.setItem('bow.locale', 'he'));
  await page.reload();
  await expect(page.getByText('מסד נתוני ההדגמה נוצר').last()).toBeVisible({ timeout: 60000 });
  await page.waitForTimeout(800);
  await page.getByTestId('demo-dataset-card').last().screenshot({ path: out + '06_created_he.png' });
  await page.evaluate(() => localStorage.removeItem('bow.locale'));

  // Mobile width
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.getByTestId('demo-dataset-card').last()).toBeVisible({ timeout: 60000 });
  await page.waitForTimeout(800);
  await page.getByTestId('demo-dataset-card').last().screenshot({ path: out + '07_created_mobile.png' });
  await page.setViewportSize({ width: 1360, height: 900 });

  // Reject-with-feedback flow → the agent revises and asks again.
  const report2 = await newTrainingReport('Demo data – feedback');
  await page.goto(`${BASE}/reports/${report2}`);
  await page.waitForTimeout(2500);
  await sendPrompt(page, PROMPT);
  await expect(page.getByTestId('demo-dataset-review').last()).toBeVisible({ timeout: 120000 });
  await page.getByTestId('demo-dataset-request-changes').last().click();
  await page.getByTestId('demo-dataset-feedback').last().fill('Add a budgets table so we can compare actuals vs plan.');
  await page.getByTestId('demo-dataset-card').last().screenshot({ path: out + '08_feedback.png' });
  await page.getByTestId('demo-dataset-send-feedback').last().click();
  await expect(page.getByText(/Proposal declined/).first()).toBeVisible({ timeout: 60000 });
  // Revised proposal arrives as a new review card containing the new table.
  await expect(page.getByTestId('demo-dataset-review').last()).toBeVisible({ timeout: 120000 });
  await expect(page.getByTestId('demo-dataset-card').last().locator('button', { hasText: 'budgets' })).toBeVisible();
  await page.waitForTimeout(400);
  await page.screenshot({ path: out + '09_revised_review.png' });

  if (errors.length) console.log('page errors:', errors);
  console.log('PASS: review card → untick → approve → progress → created (only ticked agent); he + mobile; reject with feedback → revised proposal.');
} catch (e) {
  await page.screenshot({ path: out + 'failure.png' }).catch(() => {});
  throw e;
} finally {
  await context.close();
  await browser.close();
}
