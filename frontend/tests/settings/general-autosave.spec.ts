import { test, expect } from '../fixtures/feature-test'

// Settings → General saves each field on its own: toggles and selects when
// they change, text when it loses focus. There is no Save button.
test('General settings save each field on change, without a Save button', async ({ page }) => {
  const cookie = (await page.context().cookies()).find(c => ['auth.token', 'auth_token'].includes(c.name))
  const authorization = 'Bearer ' + decodeURIComponent(cookie!.value).replace(/^Bearer /, '')
  const session = await page.request.get('/api/users/whoami', { headers: { authorization } })
  expect(session.ok()).toBeTruthy()
  const headers = { authorization, 'X-Organization-Id': (await session.json()).organizations[0].id }
  const readGeneral = async () => (await (await page.request.get('/api/organization/settings', { headers })).json()).config.general
  const original = await readGeneral()

  const name = 'Analyst ' + Date.now()
  try {
    await page.goto('/settings/general')
    const nameInput = page.locator('#ai-name')
    await expect(nameInput).toBeVisible({ timeout: 15000 })
    await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toHaveCount(0)

    // Two fields changed back to back: the second save must not undo the first.
    const puts: Promise<unknown>[] = []
    page.on('request', r => {
      if (r.url().endsWith('/organization/settings') && r.method() === 'PUT') puts.push(r.response())
    })
    await nameInput.fill(name)
    await page.getByTestId('bow-credit-toggle').click() // blurs the name field first
    await expect.poll(() => puts.length).toBe(2)
    await Promise.all(puts)
    await expect(page.getByText('Saved').first()).toBeVisible()

    const general = await readGeneral()
    expect(general.ai_analyst_name).toBe(name)
    expect(general.bow_credit).toBe(!original.bow_credit)

    // An empty name is not saved; the field goes back to the saved name.
    await nameInput.fill('   ')
    await nameInput.press('Enter')
    await expect(nameInput).toHaveValue(name)
    expect((await readGeneral()).ai_analyst_name).toBe(name)

    // A fresh load shows what was saved.
    await page.reload()
    await expect(page.locator('#ai-name')).toHaveValue(name, { timeout: 15000 })
  } finally {
    await page.request.put('/api/organization/settings', {
      headers,
      data: { config: { general: { ai_analyst_name: original.ai_analyst_name, bow_credit: original.bow_credit } } },
    })
  }
})

// A site added to the framing list saves on its own; removing one asks first,
// because pages on that site stop showing BOW right away.
test('framing sites save on change and removal asks for confirmation', async ({ page }) => {
  const cookie = (await page.context().cookies()).find(c => ['auth.token', 'auth_token'].includes(c.name))
  const authorization = 'Bearer ' + decodeURIComponent(cookie!.value).replace(/^Bearer /, '')
  const session = await page.request.get('/api/users/whoami', { headers: { authorization } })
  const headers = { authorization, 'X-Organization-Id': (await session.json()).organizations[0].id }
  const readOrigins = async () => (await (await page.request.get('/api/settings/frame-ancestors', { headers })).json()) as { origins: string[]; can_edit: boolean }
  const original = await readOrigins()
  test.skip(!original.can_edit, 'framing list is read-only for this admin')

  const site = `https://portal-${Date.now()}.example.com`
  try {
    await page.goto('/settings/general')
    const input = page.locator('#frame-ancestors-input')
    await expect(input).toBeVisible({ timeout: 15000 })
    await input.fill(site)
    await input.press('Enter')
    await expect.poll(async () => (await readOrigins()).origins).toContain(site)

    const chip = page.getByTestId('frame-ancestors-item').filter({ hasText: site })
    await chip.getByRole('button').click()
    await expect(page.getByTestId('frame-ancestors-confirm')).toBeVisible()
    await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(chip).toBeVisible()
    expect((await readOrigins()).origins).toContain(site)

    await chip.getByRole('button').click()
    await page.getByTestId('frame-ancestors-confirm-remove').click()
    await expect(chip).toHaveCount(0)
    await expect.poll(async () => (await readOrigins()).origins).not.toContain(site)
  } finally {
    await page.request.put('/api/settings/frame-ancestors', { headers, data: { origins: original.origins } })
  }
})
