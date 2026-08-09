import { test, expect } from '../fixtures/feature-test';

/**
 * The prompt box's agent scope is a MODE, not a set (see
 * frontend/utils/agentSelection.ts): Auto means "no agent pinned", and the
 * backend resolves it per run. Rendering Auto as "every agent selected" both
 * forces the user to deselect their way down to one agent and pins today's
 * whole roster onto every report the home page creates.
 */

const trigger = (page) => page.getByTestId('agent-scope-trigger').first();

async function openScopePanel(page) {
  // The panel stays open after picking a row, so close anything already open
  // before toggling — otherwise the click just dismisses it.
  //
  // Escape, not a click at a fixed coordinate: the top of the viewport can be
  // covered by the fixed onboarding banner (layouts/default.vue), which is a
  // full-width link to the onboarding wizard. Clicking blind at (20, 20) hit
  // it on CI and navigated away from the prompt box entirely.
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  // If Escape didn't take, the panel is still open — toggling now would close
  // it, so only click when it's actually shut.
  const auto = page.getByTestId('agent-scope-auto');
  if (!(await auto.isVisible().catch(() => false))) {
    await trigger(page).click();
  }
  await expect(auto).toBeVisible({ timeout: 15000 });
}

test('the prompt box opens on Auto, with no agent pinned', async ({ page }) => {
  await page.goto('/', { waitUntil: 'commit' });
  await page.waitForLoadState('domcontentloaded');
  await expect(trigger(page)).toBeVisible({ timeout: 20000 });

  await openScopePanel(page);
  const options = page.getByTestId('agent-scope-option');
  if ((await options.count()) === 0) test.skip(true, 'workspace has no agents to scope');

  await expect(page.getByTestId('agent-scope-auto')).toHaveAttribute('data-selected', 'true');
  // The point of the test: Auto is empty. Not one row is checked.
  await expect(options.filter({ has: page.locator('[data-selected="true"]') })).toHaveCount(0);
  for (const option of await options.all()) {
    await expect(option).toHaveAttribute('data-selected', 'false');
  }
});

test('picking one agent from Auto pins exactly that agent', async ({ page }) => {
  await page.goto('/', { waitUntil: 'commit' });
  await page.waitForLoadState('domcontentloaded');
  await expect(trigger(page)).toBeVisible({ timeout: 20000 });

  await openScopePanel(page);
  const options = page.getByTestId('agent-scope-option');
  if ((await options.count()) === 0) test.skip(true, 'workspace has no agents to scope');

  const target = options.first();
  const name = await target.getAttribute('data-agent-name');
  await target.click();
  await page.waitForTimeout(1000);

  await openScopePanel(page);
  await expect(page.getByTestId('agent-scope-auto')).toHaveAttribute('data-selected', 'false');
  await expect(page.locator('[data-testid="agent-scope-option"][data-selected="true"]')).toHaveCount(1);
  await expect(page.locator(`[data-testid="agent-scope-option"][data-agent-name="${name}"]`))
    .toHaveAttribute('data-selected', 'true');

  // ...and Auto is one click back, not N.
  await page.getByTestId('agent-scope-auto').click();
  await page.waitForTimeout(1000);
  await openScopePanel(page);
  await expect(page.getByTestId('agent-scope-auto')).toHaveAttribute('data-selected', 'true');
  await expect(page.locator('[data-testid="agent-scope-option"][data-selected="true"]')).toHaveCount(0);
});
