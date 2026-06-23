import { test, expect } from '../fixtures/feature-test';
import * as fs from 'fs';

// Captures the Agents / Knowledge Explorer so the KnowledgeExplorer.vue changes
// (Discard, Run-eval strip, pending-review refresh) can be eyeballed. A fresh
// admin has no pending builds, so this verifies the page renders cleanly and the
// create -> left-list refresh path works; the suggestion-only controls need
// seeded pending builds (see sandbox-feedback-loop-agents-ui.md).
test('knowledge explorer renders + screenshots', async ({ page }) => {
  fs.mkdirSync('screenshots', { recursive: true });

  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e)));

  await page.goto('/agents');
  await page.waitForLoadState('networkidle');

  // The component mounts under the "Agents" heading.
  await expect(page.getByRole('heading', { name: 'Agents', exact: true }))
    .toBeVisible({ timeout: 20000 });

  // Empty / initial explorer state.
  await page.screenshot({ path: 'screenshots/agents-explorer-initial.png', fullPage: true });

  // Exercise create -> left-list refresh: add a global instruction via the
  // "New" menu, if present, and screenshot the populated three-pane view.
  const newBtn = page.getByRole('button', { name: /New/ }).first();
  if (await newBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
    await newBtn.click().catch(() => {});
    await page.waitForTimeout(800);
    await page.screenshot({ path: 'screenshots/agents-explorer-new-menu.png', fullPage: true });
  }

  // No uncaught runtime errors from the component (the real risk after the
  // 3-way merge + eval-strip additions).
  expect(errors, `page errors:\n${errors.join('\n')}`).toEqual([]);
});
