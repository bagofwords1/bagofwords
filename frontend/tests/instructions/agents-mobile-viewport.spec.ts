import { test, expect } from '../fixtures/feature-test';

// Regression for the mobile /agents layout (KnowledgeExplorer.vue): on a phone
// viewport the header action buttons used to sit on the same non-wrapping row
// as the title, crushing the title/subtitle into a one-word-per-line sliver,
// and the agent-overview pills/actions overflowed and painted over each other.
// The header rows now wrap, so the title block must keep a readable width and
// nothing may overflow the viewport horizontally.
test.use({ viewport: { width: 390, height: 844 } });

test('agents page lays out cleanly on a phone viewport', async ({ page }) => {
  await page.goto('/agents');
  await page.waitForLoadState('networkidle');

  const heading = page.getByRole('heading', { name: 'Agents', exact: true });
  await expect(heading).toBeVisible({ timeout: 20000 });

  // No horizontal overflow anywhere on the page.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);

  // The subtitle spans the pane instead of being crushed beside the action
  // buttons (it used to get ~90px next to "Connect Git" + "New").
  const subtitle = heading.locator('xpath=following-sibling::p[1]');
  await expect(subtitle).toBeVisible();
  const box = await subtitle.boundingBox();
  expect(box, 'subtitle should render').toBeTruthy();
  expect(box!.width).toBeGreaterThan(300);

  // The "New" button stays fully inside the viewport.
  const newBtn = page.getByRole('button', { name: /New/ }).first();
  if (await newBtn.isVisible().catch(() => false)) {
    const b = await newBtn.boundingBox();
    expect(b!.x).toBeGreaterThanOrEqual(0);
    expect(b!.x + b!.width).toBeLessThanOrEqual(390);
  }
});
