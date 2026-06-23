import { test } from '@playwright/test'

// Signs in as the pre-seeded org admin (sandbox@bow.dev / Sandbox123!).
// Caller must create this user via POST /api/auth/register on a fresh DB
// BEFORE running the spec — the first uninvited signup auto-creates the org
// with full_admin_access, which is what /evals needs.
const ADMIN = {
  email: 'sandbox@bow.dev',
  password: 'Sandbox123!',
}

test('capture empty-state illustrations', async ({ page }) => {
  await page.goto('/users/sign-in', { waitUntil: 'load' })
  await page.waitForSelector('#email', { state: 'visible', timeout: 30_000 })
  await page.fill('#email', ADMIN.email)
  await page.fill('#password', ADMIN.password)
  await page.click('button[type="submit"]')
  await page.waitForURL((url) => !url.pathname.includes('/users/sign-in'), { timeout: 20_000 })
  await page.waitForLoadState('networkidle', { timeout: 10_000 }).catch(() => {})
  console.log('after-signin url:', page.url())

  // skip onboarding if shown
  const skipBtn = page.getByRole('button', { name: /skip onboarding/i })
  if (await skipBtn.isVisible().catch(() => false)) {
    await skipBtn.click()
    await page.waitForLoadState('domcontentloaded')
    await page.waitForTimeout(500)
  }

  const shot = async (path: string, file: string, expectedText: RegExp) => {
    for (let attempt = 0; attempt < 3; attempt++) {
      await page.goto(path, { waitUntil: 'load' })
      await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {})
      await page.waitForTimeout(2500)
      if (await page.getByText(expectedText).first().isVisible().catch(() => false)) break
      await page.waitForTimeout(1500)
    }
    await page.screenshot({ path: `screenshots/${file}`, fullPage: true })
  }

  // visit /scheduled-tasks first to warm the default layout
  await shot('/scheduled-tasks', 'empty-scheduled.png', /Nothing scheduled/i)
  await shot('/evals', 'empty-evals.png', /No tests yet/i)
  await shot('/queries', 'empty-queries.png', /Nothing published/i)
})
