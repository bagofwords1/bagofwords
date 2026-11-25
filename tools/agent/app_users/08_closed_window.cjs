// The sign-in window is closed right after sign-up: it never asks for its
// code. The account must still be an app user when that browser opens BOW.
const { open, check, done, BOW } = require('./lib.cjs')
const out = process.argv[2]
const email = `noa.${Date.now() % 100000}@acme.example`
const PORTAL = process.env.PORTAL_URL || 'http://localhost:4173'
;(async () => {
  const { browser, context } = await open()
  const page = await context.newPage(); page.setDefaultTimeout(180000)
  await page.goto(`${PORTAL}/signin?user=alice&next=/portal`)
  const frame = await (await page.locator('iframe').first().elementHandle()).contentFrame()
  await frame.locator('[data-embed-status="sign_in_required"]').waitFor()
  const [popup] = await Promise.all([context.waitForEvent('page'), frame.getByTestId('embedded-artifact-sign-in').click()])
  popup.setDefaultTimeout(180000)
  await popup.waitForURL(/users\/sign-in/)
  // Sign up by the API from inside the window, then close it: the window's
  // own flow (return to /embed-signin, ask for the code) never runs.
  const registered = await popup.evaluate(async (email) => {
    // As the sign-up form does: ask for the intent, send it with this one request.
    const host = /host=(host_[a-f0-9]+)/.exec(decodeURIComponent(location.href))[1]
    const { token } = await (await fetch(`/api/embed/${host}/sso/intent`, { method: 'POST' })).json()
    const r = await fetch('/api/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Embed-Signup': token },
      body: JSON.stringify({ name: 'Noa Closed', email, password: 'Password123!' }) })
    const login = await fetch('/api/auth/jwt/login', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ username: email, password: 'Password123!' }) })
    return { register: r.status, token: (await login.json()).access_token }
  }, email)
  check('they registered inside the window', registered.register === 201 && !!registered.token, registered.register)
  await popup.close()
  await context.addCookies([{ name: 'auth.token', value: registered.token, url: BOW }])

  const bow = await context.newPage(); bow.setDefaultTimeout(180000)
  await bow.goto(BOW + '/')
  await bow.waitForURL(/\/apps$/, { timeout: 120000 }).catch(() => {})
  check('opening BOW afterwards lands on the launcher', bow.url().endsWith('/apps'), bow.url())
  const direct = await bow.evaluate(async (t) => {
    const who = await (await fetch('/api/users/whoami', { headers: { Authorization: 'Bearer ' + t } })).json()
    const org = who.organizations[0]
    const r = await fetch('/api/reports', { headers: { Authorization: 'Bearer ' + t, 'X-Organization-Id': org.id } })
    return { role: org.role, status: r.status }
  }, registered.token)
  check('the account is an app user and the workspace API refuses it', direct.role === 'app_user' && direct.status === 403, JSON.stringify(direct))
  await bow.screenshot({ path: `${out}/14-closed-window-still-app-user.png` })
  await browser.close(); done(`${out}/08-results.json`); process.exit(process.exitCode || 0)
})().catch(e => { console.error(e); process.exit(2) })
