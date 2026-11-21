// Open a connection's "Manage connection" modal the way a person would:
// Agents → Connections bar → connection icon → "Manage connection".
import { ORIGIN } from './lib.mjs';
export async function openManageModal(page, connIndex = 1) {
  await page.goto(`${ORIGIN}/agents`);
  const bar = page.locator('div.border-t', { has: page.getByText('Connections', { exact: true }) }).last();
  await bar.locator('button').first().waitFor({ timeout: 60000 });
  const icons = bar.locator('button.relative');
  await icons.nth(connIndex).click();
  const manage = page.getByRole('button', { name: 'Manage connection' });
  await manage.waitFor({ timeout: 20000 });
  await page.waitForTimeout(500);
  return manage;
}
