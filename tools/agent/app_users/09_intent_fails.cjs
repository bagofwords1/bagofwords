// The intent request fails (rate limit) while someone signs up inside a
// host application's sign-in window: the registration must NOT be sent, the
// person is told, and trying again works once the intent can be had.
const { open, check, done, BOW } = require('./lib.cjs')
const out = process.argv[2]
const email = `lia.${Date.now() % 100000}@acme.example`
const PORTAL = process.env.PORTAL_URL || 'http://localhost:4173'
;(async () => {
  const { browser, context } = await open()
  const page = await context.newPage(); page.setDefaultTimeout(180000)
  await page.goto(`${PORTAL}/signin?user=alice&next=/portal`)
  const frame = await (await page.locator('iframe').first().elementHandle()).contentFrame()
  await frame.locator('[data-embed-status="sign_in_required"]').waitFor()
  const [popup] = await Promise.all([context.waitForEvent('page'), frame.getByTestId('embedded-artifact-sign-in').click()])
  popup.setDefaultTimeout(180000)
  const sent = []
  popup.on('request', r => { if (/\/api\/auth\/register/.test(r.url())) sent.push(r.method()) })
  await popup.waitForURL(/users\/sign-in/)
  await popup.locator('a[href*="/users/sign-up"]').first().click()
  await popup.waitForURL(/users\/sign-up/)
  let blocked = true
  await popup.route('**/api/embed/*/sso/intent', route => blocked
    ? route.fulfill({ status: 429, json: { detail: 'Too many requests' } }) : route.continue())
  await popup.fill('#name', 'Lia Retry'); await popup.fill('#email', email); await popup.fill('#password', 'Password123!')
  await popup.click('button[type=submit]')
  await popup.getByText("Couldn't confirm the app you're signing in to").waitFor({ timeout: 30000 })
  await popup.waitForTimeout(1500)
  check('with the intent request failing, the person is told and stays on the form', /users\/sign-up/.test(popup.url()))
  check('...and no registration request was sent', sent.length === 0, sent.join(','))
  await popup.screenshot({ path: `${out}/15-intent-failed-nothing-sent.png` })
  const exists = await popup.evaluate(async (email) => {
    const r = await fetch('/api/auth/jwt/login', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ username: email, password: 'Password123!' }) })
    return r.status
  }, email)
  check('...so no account exists', exists !== 200, exists)

  blocked = false
  await popup.click('button[type=submit]')
  await popup.waitForEvent('close', { timeout: 180000 })
  check('trying again registers and the window completes', sent.length === 1, sent.join(','))
  const bow = await context.newPage(); bow.setDefaultTimeout(180000)
  await bow.goto(BOW + '/'); await bow.waitForURL(/\/apps$/, { timeout: 120000 }).catch(() => {})
  check('the account it then created is an app user', bow.url().endsWith('/apps'), bow.url())
  await browser.close(); done(`${out}/09-results.json`); process.exit(process.exitCode || 0)
})().catch(e => { console.error(e); process.exit(2) })
