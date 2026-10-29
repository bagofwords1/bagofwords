// Runs against the isolated HTTPS stack and tools/agent/saml_test_idp.py.
import { chromium, expect } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
const output = new URL('../../../media/pr/saml-sso/', import.meta.url).pathname
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 1360, height: 900 }, recordVideo: { dir: `${output}video` } })
const page = await context.newPage()
try {
  await page.goto('https://localhost:3000/users/sign-in')
  await expect(page.getByRole('button', { name: 'Sign in with Local SAML', exact: true })).toBeVisible()
  await page.screenshot({ path: `${output}after.png` })
  const settings = await (await page.request.get('https://localhost:3000/api/settings')).json()
  const publicProvider = settings.saml_providers.find(p => p.name === 'local-saml')
  expect(Object.keys(publicProvider).sort()).toEqual(['brand', 'enabled', 'label', 'name', 'protocol'])
  await page.getByRole('button', { name: 'Sign in with Local SAML', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Local test identity provider' })).toBeVisible()
  await page.getByRole('button', { name: 'Continue to BOW' }).click()
  await page.waitForURL(url => url.origin === 'https://localhost:3000' && !url.pathname.startsWith('/users/'), { timeout: 45000 })
  await expect(page.locator('body')).not.toContainText('SSO sign-in failed')
  await expect(page.locator('h1')).toBeVisible({ timeout: 45000 })
  await page.screenshot({ path: `${output}local-signed-in.png` })
  // Browser's real session cookie must authorize the current-user endpoint.
  const cookies = await context.cookies('https://localhost:3000')
  const token = cookies.find(c => c.name.includes('auth.token'))?.value
  expect(token).toBeTruthy()
  const who = await page.request.get('https://localhost:3000/api/users/whoami', { headers: { Authorization: `Bearer ${decodeURIComponent(token)}` } })
  expect(who.status()).toBe(200)
  const user = await who.json()
  expect(user.email).toBe('local-saml@example.com')
  expect(user.organizations).toHaveLength(1)
  expect(user.organizations[0].role).toBe('member')
  // Error state is localized and does not disclose protocol internals.
  await context.clearCookies()
  await page.goto('https://localhost:3000/users/sign-in?error_code=saml_login_failed')
  await expect(page.getByText('SSO sign-in failed. Try again or contact your administrator.', { exact: true })).toBeVisible()
  await page.screenshot({ path: `${output}error.png` })
  await page.evaluate(() => localStorage.setItem('bow.locale', 'he'))
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
  await expect(page.getByRole('button', { name: /Local SAML/ })).toBeVisible()
  await page.screenshot({ path: `${output}he.png` })
  await page.evaluate(() => localStorage.setItem('bow.locale', 'en'))
  // Exercise single-provider embedded SSO using the real SAML endpoint. Only
  // public display configuration is varied; protocol/session routes stay real.
  await page.route('**/api/settings', async route => {
    const response = await route.fetch()
    const data = await response.json()
    data.auth.mode = 'sso_only'
    data.saml_providers = data.saml_providers.filter(p => p.name === 'local-saml')
    data.oidc_providers = []
    data.google_oauth.enabled = false
    await route.fulfill({ response, json: data })
  })
  await page.goto('https://localhost:3000/users/sign-in?login_hint=local-saml%40example.com')
  await expect(page.getByRole('heading', { name: 'Local test identity provider' })).toBeVisible()
  await page.goto('https://localhost:3000/users/sign-in?login_hint=local-saml%40example.com&error_code=saml_login_failed')
  await expect(page.getByText('SSO sign-in failed. Try again or contact your administrator.', { exact: true })).toBeVisible()
  expect(new URL(page.url()).pathname).toBe('/users/sign-in')
  await page.goto('https://localhost:3000/users/sign-up')
  await expect(page.getByRole('button', { name: 'Continue with Local SAML', exact: true })).toBeVisible()
  console.log('PASS: provider discovery, signed SAML POST, member session, localized failure, Hebrew RTL, embedded SSO, error-loop guard, signup')
} finally {
  await context.close()
  await browser.close()
}
