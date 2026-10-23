import { test, expect } from '../fixtures/feature-test';

test('diagnosis clear button removes every filter and reruns', async ({ page }) => {
  await page.goto('/monitoring/diagnosis?q=status:error%20feedback:negative', { waitUntil: 'commit' });

  const bar = page.getByRole('textbox', { name: 'Query' });
  await expect(bar).toHaveValue('status:error feedback:negative', { timeout: 30000 });

  const clear = page.getByTestId('query-clear');
  await expect(clear).toBeVisible();
  await clear.click();

  await expect(bar).toHaveValue('');
  await expect(clear).toBeHidden();
  // The committed query left the URL too, so a reload stays unfiltered.
  await expect(page).toHaveURL(url => !url.searchParams.has('q'));
});
