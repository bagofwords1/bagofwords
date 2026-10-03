// Live opt-in Entra test. Credentials are read only from environment variables.
// No traces, HARs, storage state or login-page videos are retained.
import { chromium, expect } from '@playwright/test'
const password = process.env.BOW_SAML_DEMO_PASSWORD
const users = (process.env.BOW_SAML_DEMO_USERS || '').split(',').filter(Boolean)
if (!password || !users.length) throw new Error('Set BOW_SAML_DEMO_USERS and BOW_SAML_DEMO_PASSWORD')
const browser = await chromium.launch({ headless: true })
try {
  for (const [index, email] of users.entries()) {
    const context = await browser.newContext({ viewport: { width: 1360, height: 900 } })
    const page = await context.newPage()
    await page.goto('https://localhost:3000/users/sign-in')
    await page.getByRole('button', { name: 'Sign in with BOW SAML Demo', exact: true }).click()
    await page.waitForURL('https://login.microsoftonline.com/**')
    await page.locator('input[type=email]').fill(email)
    await page.locator('input[type=submit]').click()
    await page.locator('input[type=password]').waitFor({ state: 'visible' })
    await page.locator('input[type=password]').fill(password)
    await page.locator('input[type=submit]').click()
    // Only the known "Stay signed in?" dialog is automated. MFA or account
    // changes require the user; do not record potentially sensitive screens.
    const outcome = await Promise.race([
      page.waitForURL('https://localhost:3000/**', { timeout: 60000 }).then(() => 'callback'),
      page.getByText('Stay signed in?', { exact: true }).waitFor({ timeout: 60000 }).then(() => 'stay'),
    ]).catch(() => 'blocked')
    if (outcome === 'stay') await page.getByRole('button', { name: 'No', exact: true }).click()
    if (outcome === 'blocked') {
      console.log(`User ${index + 1}: Entra did not return to BOW; page title: ${await page.title()}`)
      // Save text locally for diagnosis, excluding filled input values.
      const { writeFile } = await import('node:fs/promises')
      await writeFile(`/tmp/bow-saml-entra-block-${index + 1}.txt`, await page.locator('body').innerText(), { mode: 0o600 })
      throw new Error('Entra needs attention; inspect local diagnostic text')
    }
    await page.waitForURL(url => url.origin === 'https://localhost:3000' && !url.pathname.startsWith('/users/'), { timeout: 45000 })
    const cookies = await context.cookies('https://localhost:3000')
    const token = cookies.find(c => c.name.includes('auth.token'))?.value
    expect(token).toBeTruthy()
    const who = await page.request.get('https://localhost:3000/api/users/whoami', { headers: { Authorization: `Bearer ${decodeURIComponent(token)}` } })
    expect(who.status()).toBe(200)
    const user = await who.json()
    expect(user.email.toLowerCase()).toBe(email.toLowerCase())
    expect(user.organizations).toHaveLength(1)
    expect(user.organizations[0].role).toBe('member')
    await expect(page.locator('h1')).toBeVisible({ timeout: 45000 })
    await page.screenshot({ path: new URL(`../../../media/pr/saml-sso/entra-user-${index + 1}.png`, import.meta.url).pathname })
    console.log(`PASS: Entra test user ${index + 1} authenticated as a BOW organization member`)
    await context.close()
  }
} finally { await browser.close() }
