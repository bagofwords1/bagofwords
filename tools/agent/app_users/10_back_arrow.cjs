// The /d back arrow for a workspace member: it returns to the list the
// dashboard was opened from (the dashboards list with its search, or the
// launcher), and goes home on a direct link. Then the launcher in Hebrew (RTL).
// Run after seed_apps.py, as the admin (a workspace member).
const { open, signIn, check, done, BOW } = require('./lib.cjs')
const out = process.argv[2]
const email = process.env.BOW_ADMIN_EMAIL || 'admin@example.com'
const href = page => page.getByTestId('app-back').getAttribute('href')
const label = page => page.getByTestId('app-back').getAttribute('aria-label')
;(async () => {
  const { browser, context } = await open()
  const { page, api } = await signIn(context, email)

  // From the dashboards list, searched: back returns to that very list.
  await page.goto(`${BOW}/dashboards?q=Quarterly`)
  const card = page.locator('a[href*="/d/"]', { hasText: 'Quarterly Review' }).first()
  await card.waitFor()
  check('a dashboards-list card names its origin', /[?&]back=dashboards\b/.test(await card.getAttribute('href') || ''), await card.getAttribute('href'))
  await card.click(); await page.waitForURL(/\/d\/.*back=dashboards/)
  await page.getByTestId('app-back').waitFor(); await page.waitForTimeout(2500)
  check('back points at the searched dashboards list', await href(page) === '/dashboards?q=Quarterly', await href(page))
  check('labelled "Back to dashboards"', await label(page) === 'Back to dashboards', await label(page))
  await page.getByTestId('app-back').hover(); await page.waitForTimeout(600)
  await page.screenshot({ path: `${out}/10-member-back-to-dashboards.png`, clip: { x: 0, y: 0, width: 520, height: 160 } })
  await page.getByTestId('app-back').click(); await page.waitForURL(/\/dashboards\?q=Quarterly$/)
  check('back returns to the list with its search', true)

  // A direct link names no origin: home, as before.
  const target = new URL(await card.getAttribute('href'), BOW).pathname
  await page.goto(BOW + target); await page.getByTestId('app-back').waitFor()
  check('on a direct link back leads home', await href(page) === '/', await href(page))
  check('labelled "Back to home", not "Back to app"', await label(page) === 'Back to home', await label(page))
  await page.goto(`${BOW}${target}?back=https://evil.example`); await page.getByTestId('app-back').waitFor()
  check('an unknown origin is ignored', await href(page) === '/', await href(page))

  // From the launcher, which a member may open too: back to it.
  await page.goto(`${BOW}/apps`); await page.locator('[data-testid="app-launcher"][data-state="ready"]').waitFor()
  const titles = await page.locator('[data-testid="app-card-title"]').allTextContents()
  check('a member sees the launcher by the same rule (their own items too)', ['Board Memo', 'Inventory Lookup', 'Onboarding Notes', 'Quarterly Review'].every(t => titles.includes(t)), titles)
  await page.locator('[data-testid="app-card"]', { hasText: 'Quarterly Review' }).click()
  await page.waitForURL(/\/d\/.*back=apps/); await page.getByTestId('app-back').waitFor()
  check('from the launcher back points at it', await href(page) === '/apps', await href(page))
  check('labelled "Back to Shared with you"', await label(page) === 'Back to Shared with you', await label(page))

  // Hebrew: the launcher and its filter, right to left.
  await page.evaluate(() => localStorage.setItem('bow.locale', 'he'))
  await page.goto(`${BOW}/apps`); await page.locator('[data-testid="app-launcher"][data-state="ready"]').waitFor()
  await page.waitForTimeout(1500)
  check('Hebrew is right to left', await page.locator('html').getAttribute('dir') === 'rtl')
  check('the heading is "משותף איתך"', (await page.locator('h1').textContent()).trim() === 'משותף איתך', await page.locator('h1').textContent())
  await page.screenshot({ path: `${out}/11-launcher-he.png` })
  await page.locator('[data-testid="app-card"]', { hasText: 'Onboarding Notes' }).click()
  await page.getByTestId('app-back').waitFor(); await page.waitForTimeout(2000)
  await page.getByTestId('app-back').hover(); await page.waitForTimeout(600)
  await page.screenshot({ path: `${out}/12-back-he.png`, clip: { x: 920, y: 0, width: 520, height: 160 } })
  check('the Hebrew back label', await label(page) === 'חזרה ל״משותף איתך״', await label(page))
  await page.evaluate(() => localStorage.removeItem('bow.locale'))

  const errors = api.filter(l => /^CONSOLE/.test(l) && !/favicon|Failed to load resource|Not found parent scope/.test(l))
  check('no console errors / i18n warnings', errors.length === 0, errors.slice(0, 3).join(' | '))
  await browser.close(); done(`${out}/10-results.json`)
})().catch(e => { console.error(e); process.exit(2) })
