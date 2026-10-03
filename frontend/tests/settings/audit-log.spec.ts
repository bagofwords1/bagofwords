import { test, expect } from '../fixtures/feature-test';

// Audit Logs page layout (docs/design/audit-log-streams.md, Loop A5).
// Invariant: no action cell may overflow into the next column — it either
// fits or truncates with the full action in its tooltip — and clicking a row
// opens the detail drawer. Runs in en and he (RTL).
//
// The page is enterprise-gated; CI's Playwright stack runs unlicensed, so
// the spec skips when the gate is shown. The licensed run lives in
// tools/agent/audit_ui_flow.mjs (sandbox) and is recorded in
// docs/feedback-loops/audit-log-streams.md.

for (const locale of ['en', 'he']) {
  test(`audit rows never overflow and open the drawer (${locale})`, async ({ page }) => {
    await page.evaluate((l) => localStorage.setItem('bow.locale', l), locale);
    await page.goto('/settings/audit', { waitUntil: 'commit' });
    await page.waitForLoadState('domcontentloaded');

    const gate = page.getByText(/enterprise license|רישיון ארגוני/i);
    const firstRow = page.getByTestId('audit-row').first();
    await expect(gate.or(firstRow)).toBeVisible({ timeout: 20000 });
    test.skip(await gate.isVisible(), 'Audit logs need an enterprise license on this stack');

    const overflowing = await page.$$eval('[data-testid="audit-action"]', (els) =>
      els.filter((el) => el.scrollWidth > el.clientWidth + 1 && !el.getAttribute('title')).map((el) => el.textContent?.trim()),
    );
    expect(overflowing).toEqual([]);
    for (const title of await page.$$eval('[data-testid="audit-action"]', (els) => els.map((el) => el.getAttribute('title')))) {
      expect(title).toBeTruthy();
    }

    await firstRow.click();
    const drawer = page.getByTestId('audit-drawer');
    await expect(drawer).toBeVisible();
    await expect(drawer.getByTestId('audit-field-actor')).not.toBeEmpty();
    await page.keyboard.press('Escape');
    await expect(drawer).toBeHidden();
  });
}
