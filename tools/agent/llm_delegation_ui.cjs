// Browser flows for delegated LLM management (see llm_delegation_live.py).
//
//   node tools/agent/llm_delegation_ui.cjs flow  <outDir> [locale]   # every role, PASS/FAIL per step
//   node tools/agent/llm_delegation_ui.cjs before <outDir>           # what the same people saw before
//
// Needs the sandbox seeded (`llm_delegation_live.py seed`) and the frontend
// on :3000. Screenshots land in <outDir>; one line per check on stdout.
const { chromium } = require('playwright')
const fs = require('fs')
const path = require('path')

const BASE = process.env.BOW_UI_BASE || 'http://localhost:3000'
const PASSWORD = 'Password123!'
const [, , mode = 'flow', outDir = 'media/llm-delegation', locale = 'en'] = process.argv
fs.mkdirSync(outDir, { recursive: true })

let passed = 0, failed = 0
const check = (ok, label, extra = '') => {
  ok ? passed++ : failed++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`)
}
const shot = async (page, name) => page.screenshot({ path: path.join(outDir, `${name}.png`), fullPage: true })

async function session(browser, email) {
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2, locale })
  const page = await ctx.newPage()
  if (locale !== 'en') {
    await page.addInitScript((l) => { try { localStorage.setItem('bow.locale', l) } catch (e) {} }, locale)
  }
  await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'domcontentloaded', timeout: 240000 })
  await page.waitForSelector('input[type=password]', { timeout: 240000 })
  await page.fill('input[type=email]', email)
  await page.fill('input[type=password]', PASSWORD)
  await page.keyboard.press('Enter')
  await page.waitForURL(u => !u.toString().includes('sign-in'), { timeout: 120000 })
  await page.waitForTimeout(1500)
  return { ctx, page }
}

async function adminEntry(page) {
  // The sidebar's Admin entry: a link into /settings outside the admin console.
  const link = page.locator('a[href^="/settings"]').first()
  return (await link.count()) ? link.getAttribute('href') : null
}

async function openModels(page) {
  await page.goto(`${BASE}/settings/models`, { waitUntil: 'domcontentloaded', timeout: 240000 })
  await page.waitForSelector('[data-testid=model-row]', { timeout: 240000 })
  await page.waitForTimeout(1200)
}

async function providerChips(page) {
  return page.locator('[data-testid=provider-chip]').allInnerTexts()
}

async function openGroup(page, name) {
  await page.goto(`${BASE}/settings/members?tab=groups`, { waitUntil: 'domcontentloaded', timeout: 240000 })
  await page.getByText(name, { exact: true }).first().waitFor({ timeout: 240000 })
  // The members dialog opens from the group's member-count button.
  await page.locator('tr', { hasText: name }).locator('button.tabular-nums').first().click()
  await page.waitForTimeout(3000)
}

async function flow() {
  const browser = await chromium.launch()
  const sfx = locale === 'en' ? '' : `-${locale}`

  // ── Alice: Finance group manager + OpenAI (Finance) provider manager ──
  {
    const { ctx, page } = await session(browser, 'alice@example.com')
    const href = await adminEntry(page)
    check(href && href.startsWith('/settings/models'), 'alice: Admin entry opens the LLM page', `href=${href}`)
    await openModels(page)
    await shot(page, `after-alice-models${sfx}`)
    const chips = await providerChips(page)
    check(chips.length === 1 && chips[0].includes('OpenAI (Finance)'), 'alice: sees only her provider', JSON.stringify(chips))
    const providers = await page.locator('[data-testid=llm-provider-cell]').allInnerTexts()
    check(providers.length > 0 && providers.every(p => p.includes('OpenAI (Finance)')), 'alice: every row is her provider\'s model', `${providers.length} rows`)
    check(await page.locator('[data-testid=provider-manager-banner]').isVisible(), 'alice: "You manage" banner shown')
    check(!(await page.locator('[data-testid=add-provider-button]').count()), 'alice: no Add provider button')
    check(!(await page.locator('[data-testid=add-model-button]').count()), 'alice: no Add model button')
    check(!(await page.locator('[data-testid=llm-access-cell]').count()), 'alice: no Access column')
    check(await page.locator('[data-testid=auto-router-switch]').isDisabled().catch(() => true), 'alice: Auto router switch locked')
    check(!(await page.locator('[data-testid=llm-group-default-row]').count()), 'alice: no group defaults table')

    // Routing hint on GPT-6 Sol, through the table.
    const solRow = page.locator('[data-testid=model-row]', { hasText: 'GPT-6 Sol' }).first()
    await solRow.locator('[data-testid=llm-routing-cell] button').first().click()
    await solRow.locator('textarea').fill('Finance month-end close and variance analysis')
    await solRow.locator('[data-testid=llm-routing-cell] button').first().click()
    await page.waitForTimeout(1500)
    check((await solRow.innerText()).includes('Finance month-end close'), 'alice: saved a routing hint on GPT-6 Sol')

    // Model card: no defaults, no delete, prices read-only.
    await solRow.locator('td').first().click()
    await page.waitForSelector('[data-testid=card-save-button]', { timeout: 30000 })
    await page.waitForTimeout(600)
    await shot(page, `after-alice-model-card${sfx}`)
    check(!(await page.locator('[data-testid=card-make-default]').count()), 'alice: card has no "Make default"')
    check(!(await page.locator('[data-testid=card-delete-button]').count()), 'alice: card has no Delete')
    check(await page.locator('[data-testid=card-cost-input]').isDisabled(), 'alice: card price is read-only')
    await page.keyboard.press('Escape')
    await page.waitForTimeout(600)

    // Provider dialog: settings + models, no add/delete/managers.
    await page.locator('[data-testid=provider-chip-gear]').first().click()
    await page.waitForTimeout(2500)
    await shot(page, `after-alice-provider-dialog${sfx}`)
    const dlg = await page.locator('[role=dialog]').last().innerText()
    check(!dlg.includes('Add Custom Model'), 'alice: provider dialog has no "Add Custom Model"')
    check(!dlg.includes('Danger Zone'), 'alice: provider dialog has no Danger Zone')
    check(!(await page.locator('[data-testid=provider-managers]').count()), 'alice: provider dialog has no Managers section')
    await page.keyboard.press('Escape')
    await page.waitForTimeout(600)

    // Groups → Finance → default models.
    await openGroup(page, 'Finance')
    const sel = page.locator('[data-testid=group-default-model-select]')
    await sel.waitFor({ timeout: 60000 })
    const opts = await sel.locator('option').allInnerTexts()
    check(opts.some(o => o.includes('GPT-6 Luna')) && !opts.some(o => o.includes('Gemini')), 'alice: Finance choices are Finance\'s models, no Gemini', JSON.stringify(opts))
    const luna = (await sel.locator('option', { hasText: 'GPT-6 Luna ·' }).first().getAttribute('value'))
    await sel.selectOption(luna)
    await page.waitForTimeout(1500)
    check((await sel.inputValue()) === luna, 'alice: set Finance\'s default to GPT-6 Luna')
    await shot(page, `after-alice-group-models${sfx}`)
    await ctx.close()
  }

  // ── Bob: Marketing group + Gemini (Marketing) provider manager ──
  {
    const { ctx, page } = await session(browser, 'bob@example.com')
    await openModels(page)
    const chips = await providerChips(page)
    check(chips.length === 1 && chips[0].includes('Gemini (Marketing)'), 'bob: sees only his provider', JSON.stringify(chips))
    await shot(page, `after-bob-models${sfx}`)
    await ctx.close()
  }

  // ── Fiona: plain Finance member ──
  {
    const { ctx, page } = await session(browser, 'fiona@example.com')
    check(!(await adminEntry(page)), 'fiona: no Admin entry')
    await page.goto(`${BASE}/settings/models`, { waitUntil: 'domcontentloaded', timeout: 240000 })
    await page.waitForTimeout(4000)
    check(!page.url().includes('/settings/models'), 'fiona: /settings/models sends her away', page.url())
    await ctx.close()
  }

  // ── Admin: managers on the provider, decision model note ──
  {
    const { ctx, page } = await session(browser, 'admin@example.com')
    await openModels(page)
    await shot(page, `after-admin-models${sfx}`)
    const chip = page.locator('[data-testid=provider-chip]', { hasText: 'Gemini (Marketing)' })
    await chip.locator('[data-testid=provider-chip-gear]').click()
    await page.waitForSelector('[data-testid=provider-managers]', { timeout: 60000 })
    await page.waitForTimeout(1500)
    const rows = await page.locator('[data-testid=provider-manager-row]').allInnerTexts()
    check(rows.some(r => r.includes('Bob')), 'admin: Gemini (Marketing) lists Bob as manager', JSON.stringify(rows))
    await page.locator('[data-testid=provider-managers]').scrollIntoViewIfNeeded()
    await shot(page, `after-admin-provider-managers${sfx}`)
    // Start clean if an earlier run left Mark on, then add and remove him.
    const stale = page.locator('[data-testid=provider-manager-row]', { hasText: 'mark@example.com' })
    if (await stale.count()) { await stale.locator('[data-testid=provider-manager-remove]').click(); await page.waitForTimeout(1500) }
    // Add Mark, then remove him again.
    await page.locator('[data-testid=provider-manager-select]').click()
    await page.keyboard.type('mark')
    await page.waitForTimeout(500)
    await page.keyboard.press('Enter')
    await page.locator('[data-testid=provider-manager-add]').click()
    await page.waitForTimeout(1500)
    const after = await page.locator('[data-testid=provider-manager-row]').allInnerTexts()
    check(after.some(r => r.includes('mark@example.com')), 'admin: added Mark as a Gemini manager', JSON.stringify(after))
    await page.locator('[data-testid=provider-manager-row]', { hasText: 'mark@example.com' }).locator('[data-testid=provider-manager-remove]').click()
    await page.waitForTimeout(1500)
    const final = await page.locator('[data-testid=provider-manager-row]').allInnerTexts()
    check(!final.some(r => r.includes('mark@example.com')) && final.some(r => r.includes('bob@example.com')), 'admin: removed Mark, Bob stays', JSON.stringify(final))
    await page.keyboard.press('Escape')
    await page.waitForTimeout(800)
    await page.locator('[data-testid=auto-router-configure]').click()
    await page.waitForSelector('[data-testid=auto-router-decision-restricted]', { timeout: 30000 })
    check(true, 'admin: Auto router dialog warns the decision model is restricted')
    await shot(page, `after-admin-router-restricted${sfx}`)
    await ctx.close()
  }

  await browser.close()
  console.log(`${passed}/${passed + failed} checks passed`)
  process.exit(failed ? 1 : 0)
}

async function before() {
  const browser = await chromium.launch()
  const { ctx, page } = await session(browser, 'alice@example.com')
  const href = await adminEntry(page)
  console.log(`alice: Admin entry → ${href}`)
  await page.goto(`${BASE}/settings/models`, { waitUntil: 'domcontentloaded', timeout: 240000 })
  await page.waitForTimeout(5000)
  console.log(`alice: /settings/models → ${page.url()}`)
  await shot(page, 'before-alice-models')
  await openGroup(page, 'Finance')
  console.log(`alice: Finance dialog has default models: ${(await page.locator('[data-testid=group-model-defaults]').count()) > 0}`)
  await shot(page, 'before-alice-group')
  await ctx.close()
  await browser.close()
}

;({ flow, before }[mode] || flow)().catch(e => { console.error(e); process.exit(1) })
