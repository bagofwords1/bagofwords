import { expect, test } from '../fixtures/feature-test'

test('enable World, explore a folder, and disable its deep link', async ({ page }) => {
  const cookie = (await page.context().cookies()).find(c => ['auth.token', 'auth_token'].includes(c.name))
  const authorization = 'Bearer ' + decodeURIComponent(cookie!.value).replace(/^Bearer /, '')
  const session = await page.request.get('/api/users/whoami', { headers: { authorization } })
  expect(session.ok()).toBeTruthy()
  const headers = { authorization, 'X-Organization-Id': (await session.json()).organizations[0].id }
  const settings = await page.request.get('/api/organization/settings', { headers })
  const wasOn = (await settings.json()).config.show_world?.value === true
  const setEnabled = async (enabled: boolean) => {
    const result = await page.request.put('/api/organization/settings', {
      headers, data: { config: { show_world: { value: enabled } } },
    })
    expect(result.ok()).toBeTruthy()
  }
  const name = 'World folder ' + Date.now()
  const folder = await page.request.post('/api/folders', { headers, data: { name } })
  expect(folder.ok()).toBeTruthy()
  const { id } = await folder.json()
  try {
    await setEnabled(false)
    await page.goto('/agents/world')
    await expect(page).toHaveURL(/\/agents\/?$/)
    await expect(page.getByTestId('open-world')).toHaveCount(0)

    // The toggle lives in AI settings and saves on change (no Save button).
    await page.goto('/settings/ai_settings')
    await page.getByTestId('ai-settings-search').locator('input').fill('World view')
    const saved = page.waitForResponse(r => r.url().endsWith('/organization/settings') && r.request().method() === 'PUT')
    await page.getByTestId('ai-setting-show_world').getByRole('switch').click()
    expect((await saved).ok()).toBeTruthy()

    // Client-side navigation, no reload: the shared settings cache is updated.
    await page.getByRole('link', { name: 'Agents' }).first().click()
    await page.getByTestId('open-world').click()
    await expect(page.getByTestId('open-world')).toHaveAttribute('aria-current', 'page')
    await page.getByRole('switch', { name: 'Usage', exact: true }).click()
    await page.getByRole('switch', { name: 'My reports', exact: true }).click()
    await page.getByTestId('open-tree').click()
    await expect(page.getByTestId('open-tree')).toHaveAttribute('aria-current', 'page')
    await page.getByTestId('open-world').click()
    await page.getByRole('textbox', { name: 'Search loaded items…' }).fill(name)
    await page.getByRole('button', { name: name + ' Folders', exact: true }).click()
    await page.getByRole('button', { name: 'Explore inside' }).click()
    await page.getByRole('link', { name: 'Open item' }).click()
    await expect(page.getByTestId('folder-knowledge')).toContainText(name)

    await setEnabled(false)
    await page.goto('/agents/world')
    await expect(page).toHaveURL(/\/agents\/?$/)
  } finally {
    await page.request.delete('/api/folders/' + id + '?mode=keep', { headers })
    await setEnabled(wasOn)
  }
})
