import { expect, test } from '../fixtures/feature-test'

test('enable World, explore a folder, and disable its deep link', async ({ page }) => {
  const cookie = (await page.context().cookies()).find(c => ['auth.token', 'auth_token'].includes(c.name))
  const authorization = 'Bearer ' + decodeURIComponent(cookie!.value).replace(/^Bearer /, '')
  const session = await page.request.get('/api/users/whoami', { headers: { authorization } })
  expect(session.ok()).toBeTruthy()
  const headers = { authorization, 'X-Organization-Id': (await session.json()).organizations[0].id }
  const settings = await page.request.get('/api/organization/settings', { headers })
  const general = (await settings.json()).config.general
  const setEnabled = async (enabled: boolean) => {
    const result = await page.request.put('/api/organization/settings', {
      headers, data: { config: { general: { ...general, show_world: enabled } } },
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

    await page.goto('/settings/general')
    await page.getByRole('switch', { name: 'Show World in Agents' }).click()
    const saved = page.waitForResponse(r => r.url().endsWith('/organization/settings') && r.request().method() === 'PUT')
    await page.getByRole('button', { name: 'Save changes', exact: true }).click()
    expect((await saved).ok()).toBeTruthy()

    await page.goto('/agents')
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
    await setEnabled(general?.show_world === true)
  }
})
