// An admin gives an app user the workspace from the members list; the same
// browser session of that person then opens the workspace. And back.
const { open, check, done, BOW } = require('./lib.cjs')
const out = process.argv[2]
const { email } = require(out + '/../newcomer.json')
;(async () => {
  const { browser, context } = await open({ storageState: out + '/../admin-state.json' })
  const page = await context.newPage(); page.setDefaultTimeout(120000)
  const api = []
  page.on('response', r => { if (/role-assignments/.test(r.url())) api.push(`${r.status()} ${r.request().method()}`) })
  await page.goto(BOW + '/settings/members')
  const row = page.locator('tr', { hasText: email })
  await row.waitFor()
  const pick = async (label) => {
    await row.getByText(/App user|Member/).first().click()
    await page.locator('[role="option"]', { hasText: new RegExp(`^\\s*${label}\\s*$`) }).first().click()
    await page.keyboard.press('Escape'); await page.waitForTimeout(2500)
  }
  await row.getByText('App user').first().click(); await page.waitForTimeout(600)
  await page.screenshot({ path: `${out}/30-members-role-picker.png` })
  await page.keyboard.press('Escape')
  await pick('Member')
  const text = (await row.innerText()).replace(/\s+/g, ' ')
  check('picking Member replaces App user on the row', /Member/.test(text) && !/App user/.test(text), text.slice(0, 120))
  check('one assignment added, one removed', api.filter(l => /POST/.test(l)).length === 1 && api.filter(l => /DELETE/.test(l)).length === 1, api.join(' '))
  await page.screenshot({ path: `${out}/31-members-promoted.png` })

  const person = await (await open({ storageState: out + '/../newcomer-state.json' })).context
  const theirs = await person.newPage(); theirs.setDefaultTimeout(120000)
  await theirs.goto(BOW + '/')
  await theirs.waitForTimeout(8000)
  check('the promoted person now opens the workspace with the session they had', new URL(theirs.url()).pathname === '/' , theirs.url())
  await theirs.screenshot({ path: `${out}/32-promoted-workspace.png` })
  await theirs.goto(BOW + '/apps'); await theirs.locator('[data-testid="app-card"]').first().waitFor(); await theirs.waitForTimeout(1200)
  check('their launcher now links to the workspace', await theirs.getByTestId('app-launcher-open-workspace').count() === 1)
  await theirs.screenshot({ path: `${out}/33-promoted-launcher.png` })

  await pick('App user')
  const back = (await row.innerText()).replace(/\s+/g, ' ')
  check('picking App user takes the workspace away again', /App user/.test(back) && !/Member/.test(back), back.slice(0, 120))
  await theirs.goto(BOW + '/reports'); await theirs.waitForURL(/\/apps$/, { timeout: 60000 }).catch(() => {})
  check('...and their next navigation is back on the launcher', theirs.url().endsWith('/apps'), theirs.url())
  await browser.close(); done(`${out}/04-results.json`); process.exit(process.exitCode || 0)
})().catch(e => { console.error(e); process.exit(2) })
