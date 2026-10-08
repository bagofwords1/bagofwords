// Shared conversation (/c/<token>) shows create_demo_dataset live: "Waiting for
// review" while the owner reviews (read-only, no controls), then refreshes to
// the created dataset. Also: the owner's review card survives a page reload.
//
// Same prereqs as demo-dataset-flow.mjs (stack + seeded org + LLM + settings).
//   cd frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers BOW_TOKEN=… BOW_ORG=… \
//     node tests/demo_data/demo-dataset-share.mjs
import { chromium, expect } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const BASE = process.env.BOW_BASE || 'http://localhost:3000';
const API = process.env.BOW_API || 'http://localhost:8000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const TOKEN = process.env.BOW_TOKEN;
const ORG = process.env.BOW_ORG;
const PROMPT = 'Create a demo dataset for HR analytics: people, payroll and departments. Suggest agents.';
const out = fileURLToPath(new URL('../../../media/pr/demo-data-share/', import.meta.url));
mkdirSync(out, { recursive: true });

const H = { Authorization: `Bearer ${TOKEN}`, 'X-Organization-Id': ORG, 'Content-Type': 'application/json' };
async function api(path, init = {}) {
  const r = await fetch(`${API}${path}`, { ...init, headers: { ...H, ...(init.headers || {}) } });
  if (!r.ok) throw new Error(`${path} ${r.status} ${await r.text()}`);
  return r.json();
}

const browser = await chromium.launch();
const owner = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
const viewer = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
try {
  for (let i = 0; i < 4; i++) {
    await owner.goto(`${BASE}/users/sign-in`);
    try { await owner.locator('#email').waitFor({ timeout: 60000 }); break; } catch (e) { if (i === 3) throw e; }
  }
  await owner.locator('#email').fill(EMAIL);
  await owner.locator('#password').fill(PASSWORD);
  await owner.locator('button[type="submit"]').click();
  await owner.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 240000 });

  const report = await api('/api/reports', { method: 'POST', body: JSON.stringify({ title: 'Demo data – shared', mode: 'training', data_sources: [] }) });
  const share = await api(`/api/reports/${report.id}/conversation-share`, { method: 'POST' });
  const shareToken = share.token || share.conversation_share_token || share.share_token;
  if (!shareToken) throw new Error('no share token: ' + JSON.stringify(share));

  await owner.goto(`${BASE}/reports/${report.id}`);
  const box = owner.locator('[contenteditable="true"]').last();
  await box.waitFor({ timeout: 240000 });
  await owner.waitForTimeout(1500);
  await box.click();
  await box.fill(PROMPT);
  await owner.keyboard.press('Enter');
  await expect(owner.getByTestId('demo-dataset-review').last()).toBeVisible({ timeout: 120000 });

  // Viewer opens the shared link while the owner is reviewing.
  await viewer.goto(`${BASE}/c/${shareToken}`);
  const vtool = viewer.getByTestId('create-demo-dataset-tool').last();
  await expect(vtool.getByText('Waiting for review')).toBeVisible({ timeout: 120000 });
  await expect(vtool.getByTestId('demo-dataset-approve')).toHaveCount(0);
  await expect(vtool.locator('input[type="checkbox"]')).toHaveCount(0);
  await viewer.waitForTimeout(500);
  await vtool.screenshot({ path: out + '01_shared_waiting_for_review.png' });

  // Owner reloads mid-review: the card comes back with its controls.
  await owner.reload();
  await expect(owner.getByTestId('demo-dataset-review').last()).toBeVisible({ timeout: 120000 });
  await owner.getByTestId('create-demo-dataset-tool').last().screenshot({ path: out + '02_owner_review_after_reload.png' });

  await owner.getByTestId('demo-dataset-approve').last().click();
  await expect(owner.getByText('Demo dataset created').last()).toBeVisible({ timeout: 240000 });

  // The shared page refreshes on its own to the result — no reload.
  await expect(vtool.getByText('Demo dataset created')).toBeVisible({ timeout: 90000 });
  await expect(vtool.getByRole('link', { name: /Open/ })).toHaveCount(0);
  await viewer.waitForTimeout(800);
  await vtool.screenshot({ path: out + '03_shared_created.png' });
  await viewer.screenshot({ path: out + '04_shared_page.png' });
  console.log('PASS: shared view shows waiting-for-review (read-only), owner card survives reload, shared view refreshes to created.');
} catch (e) {
  await owner.screenshot({ path: out + 'failure_owner.png' }).catch(() => {});
  await viewer.screenshot({ path: out + 'failure_viewer.png' }).catch(() => {});
  throw e;
} finally {
  await browser.close();
}
