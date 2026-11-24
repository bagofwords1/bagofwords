// A brand-new person arrives through an embedded app (SSO-mode host
// application): BOW's own sign-up runs in the popup, the app appears, and
// the account is an app user. Then they go to BOW itself: the launcher.
const { open, check, done, BOW } = require('./lib.cjs')
const out = process.argv[2]
const email = process.argv[3] || `dana.${Date.now() % 100000}@acme.example`
const PORTAL = 'http://localhost:4173'
;(async () => {
  const { browser, context } = await open()
  const page = await context.newPage(); page.setDefaultTimeout(180000)
  await page.goto(`${PORTAL}/signin?user=alice&next=/portal`)
  await page.locator('iframe').first().waitFor()
  const frame = await (await page.locator('iframe').first().elementHandle()).contentFrame()
  await frame.locator('[data-embed-status="sign_in_required"]').waitFor()
  await page.screenshot({ path: `${out}/10-portal-sign-in-required.png` })
  const [popup] = await Promise.all([context.waitForEvent('page'), frame.getByTestId('embedded-artifact-sign-in').click()])
  popup.setDefaultTimeout(180000)
  const api = []
  popup.on('response', r => { if (r.url().includes('/api/')) api.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`) })
  await popup.waitForURL(/users\/sign-in/)
  check('the popup is BOW sign-in, and it knows where to return', /redirect=.*embed-signin/.test(decodeURIComponent(popup.url())), popup.url())
  await popup.locator('a[href*="/users/sign-up"]').first().waitFor()
  const signUpHref = await popup.locator('a[href*="/users/sign-up"]').first().getAttribute('href')
  check('"Sign up" keeps the way back to the popup', /redirect=/.test(signUpHref || ''), signUpHref)
  await popup.locator('a[href*="/users/sign-up"]').first().click()
  await popup.waitForURL(/users\/sign-up/)
  await popup.fill('#name', 'Dana Levi'); await popup.fill('#email', email); await popup.fill('#password', 'Password123!')
  await popup.screenshot({ path: `${out}/11-popup-sign-up.png` })
  await popup.click('button[type=submit]')
  await popup.waitForEvent('close', { timeout: 180000 })
  check('the popup closes itself after sign-up', true)
  check('the sign-up form asked for the host application\'s intent as it submitted', api.some(l => /^200 POST \/api\/embed\/host_[a-f0-9]+\/sso\/intent/.test(l)), api.filter(l => /embed/.test(l)).join(' | '))
  const jar = await context.cookies()
  check('no sign-up cookie is left in the browser', !jar.some(c => /embed_signup|_embed$/.test(c.name)), jar.map(c => c.name).join(','))
  check('...and asked for its code after', api.some(l => /^200 POST \/api\/embed\/host_[a-f0-9]+\/sso\/code/.test(l)))
  await frame.locator('[data-embed-status="ready"]').waitFor()
  await page.waitForTimeout(6000)
  await page.screenshot({ path: `${out}/12-portal-app-open.png` })
  check('the app appears inside the host product', true)

  // The same browser now holds an ordinary BOW session. Go to BOW itself.
  const bow = await context.newPage(); bow.setDefaultTimeout(180000)
  const bowApi = []
  bow.on('response', r => { if (r.url().includes('/api/')) bowApi.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`) })
  await bow.goto(BOW + '/')
  await bow.waitForURL(/\/apps$/, { timeout: 120000 }).catch(() => {})
  check('going to BOW itself lands on the launcher, not the workspace', bow.url().endsWith('/apps'), bow.url())
  await bow.locator('[data-testid="app-launcher"][data-state="ready"]').waitFor()
  await bow.locator('[data-testid="app-card"]').first().waitFor()
  await bow.waitForTimeout(1500)
  const titles = await bow.locator('[data-testid="app-card-title"]').allTextContents()
  check('the launcher shows the organization-wide app only', JSON.stringify(titles) === JSON.stringify(['Sales Assistant']), titles)
  await bow.screenshot({ path: `${out}/13-new-app-user-launcher.png` })
  // The session is real, and the API still says no.
  const token = (await context.cookies(BOW)).find(c => c.name === 'auth.token')?.value
  const direct = await bow.evaluate(async (t) => {
    const who = await (await fetch('/api/users/whoami', { headers: { Authorization: 'Bearer ' + t } })).json()
    const org = who.organizations[0]
    const r = await fetch('/api/reports', { headers: { Authorization: 'Bearer ' + t, 'X-Organization-Id': org.id } })
    return { role: org.role, permissions: org.permissions, status: r.status, body: await r.json() }
  }, token)
  check('whoami: role app_user, no permissions', direct.role === 'app_user' && direct.permissions.length === 0, JSON.stringify(direct).slice(0, 200))
  check('the API refuses the workspace with a typed error', direct.status === 403 && direct.body.error_code === 'workspace.access_required', direct.status)
  await context.storageState({ path: `${out}/../newcomer-state.json` })
  require('fs').writeFileSync(`${out}/../newcomer.json`, JSON.stringify({ email }))
  await browser.close(); done(`${out}/02-results.json`)
})().catch(e => { console.error(e); process.exit(2) })
