import { test, expect, type Page } from '@playwright/test'

// Regression: the sign-in / sign-up pages rendered `?error=` with v-html, and the
// OAuth callback reflected the provider's `error_description` into that param
// before any state check — a one-click reflected XSS on the auth pages.
const PAYLOAD = '<img src=x onerror="window.__xss=1">'

// Unauthenticated: an attacker's link lands on a logged-out sign-in page.
test.use({ storageState: { cookies: [], origins: [] } })

async function assertInert(page: Page) {
  await page.waitForLoadState('networkidle')
  expect(await page.evaluate(() => (window as any).__xss)).toBeUndefined()
  expect(await page.locator('img[src="x"]').count()).toBe(0)
}

for (const path of ['/users/sign-in', '/users/sign-up']) {
  test(`${path}: ?error= payload is not rendered as HTML`, async ({ page }) => {
    await page.goto(`${path}?error=${encodeURIComponent(PAYLOAD)}`)
    await assertInert(page)
  })
}

test('OAuth callback error_description is not reflected onto sign-in', async ({ page }) => {
  await page.goto(
    `/api/auth/google/callback?error=access_denied&error_description=${encodeURIComponent(PAYLOAD)}`,
  )
  await assertInert(page)
  expect(page.url()).not.toContain('onerror')
  expect(new URL(page.url()).searchParams.get('error_code')).toBe('oauth_login_failed')
})

test('known error_code shows its localized message; unknown codes show nothing', async ({ page }) => {
  await page.goto('/users/sign-in?error_code=invitation_required')
  await expect(page.getByText('Sign-up is disabled. Ask your admin for an invite.')).toBeVisible()

  await page.goto(`/users/sign-in?error_code=${encodeURIComponent(PAYLOAD)}`)
  await page.waitForLoadState('networkidle')
  await expect(page.locator('p.text-red-500')).toHaveCount(0)
})
