#!/usr/bin/env node
/**
 * ui-audit route sweep — Phase 1 inventory.
 *
 * Visits routes WITHOUT clicking anything and records, per route:
 *   - the final URL after redirects (a bounce is itself a finding)
 *   - every interactive control, with accessible name and a stable selector
 *   - console errors and uncaught page exceptions
 *   - failed /api/ responses during load
 *   - a full-page screenshot
 *
 * Read-only by design: it never clicks, submits, or navigates via UI. Safe to
 * run against any environment you can log into. Phase 3 does the clicking.
 */
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import fs from 'node:fs'
import path from 'node:path'

const HELP = `
ui-audit sweep — enumerate controls and load-time errors for a set of routes.

Usage:
  node sweep.mjs --routes <list|@file> [options]

Required:
  --routes <list>       Comma-separated routes, or @path/to/file with one route per line
                        (blank lines and # comments ignored).

Session (pick one):
  --login               Sign in through the UI and dismiss onboarding, then cache the
                        session at <out>/state.json and reuse it on later runs.
                        Credentials from $BOW_EMAIL / $BOW_PASSWORD, defaulting to the
                        seed_org.py admin (admin@example.com / Password123!).
  --storage <path>      Use an existing Playwright storageState JSON instead.
                        --storage none sweeps logged out (for the auth pages).
  --fresh-login         With --login, ignore a cached state.json and sign in again.
                        Use after reseeding the database.

Options:
  --out <dir>           Output directory. Default: ./audit-inventory
  --base-url <url>      Default: $PLAYWRIGHT_BASE_URL or http://localhost:3000
  --settle <ms>         Idle time to wait after hydration before reading the DOM.
                        Default 1500. Raise it for slow/animated pages.
  --hydrate-timeout <ms> How long to wait for the client to render anything at all.
                        Default 45000 — a cold "nuxt dev" compiles routes on demand
                        and a large page can take ~30s on its first hit.
  --timeout <ms>        Per-route navigation timeout. Default 30000.
  --viewport <WxH>      Default 1440x900.
  --headed              Run with a visible browser (needs a display).
  --help                This message.

Output: <out>/routes/<slug>.json, <out>/screenshots/<slug>.png, <out>/summary.json
`

function parseArgs (argv) {
  const opts = {
    routes: null,
    storage: undefined,
    out: 'audit-inventory',
    baseUrl: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000',
    settle: 1500,
    hydrateTimeout: 45000,
    timeout: 30000,
    viewport: '1440x900',
    headed: false,
    login: false,
    freshLogin: false
  }
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i]
    const next = () => {
      const v = argv[++i]
      if (v === undefined) throw new Error(`${a} requires a value`)
      return v
    }
    switch (a) {
      case '--help': case '-h': console.log(HELP); process.exit(0); break
      case '--routes': opts.routes = next(); break
      case '--storage': opts.storage = next(); break
      case '--out': opts.out = next(); break
      case '--base-url': opts.baseUrl = next(); break
      case '--settle': opts.settle = Number(next()); break
      case '--hydrate-timeout': opts.hydrateTimeout = Number(next()); break
      case '--timeout': opts.timeout = Number(next()); break
      case '--viewport': opts.viewport = next(); break
      case '--headed': opts.headed = true; break
      case '--login': opts.login = true; break
      case '--fresh-login': opts.login = true; opts.freshLogin = true; break
      default: throw new Error(`Unknown argument: ${a}\n${HELP}`)
    }
  }
  if (!opts.routes) throw new Error(`--routes is required.\n${HELP}`)
  return opts
}

function resolveRoutes (spec) {
  const raw = spec.startsWith('@')
    ? fs.readFileSync(spec.slice(1), 'utf8').split('\n')
    : spec.split(',')
  const routes = raw
    .map(r => r.trim())
    .filter(r => r && !r.startsWith('#'))
    .map(r => (r.startsWith('/') ? r : `/${r}`))
  if (!routes.length) throw new Error('No routes resolved from --routes')
  return routes
}

const repoRoot = path.resolve(fileURLToPath(import.meta.url), '../../../../..')

/** Playwright lives in frontend/node_modules, not at the repo root. */
function loadChromium () {
  const frontendPkg = path.join(repoRoot, 'frontend', 'package.json')
  if (!fs.existsSync(frontendPkg)) {
    throw new Error(`Cannot find ${frontendPkg} — run this from the bagofwords repo, or install playwright-core somewhere resolvable.`)
  }
  const require = createRequire(frontendPkg)
  try {
    return require('playwright-core').chromium
  } catch {
    return require('@playwright/test').chromium
  }
}

/**
 * The bundled playwright version and the sandbox's chromium build can disagree,
 * which makes Playwright try to download a browser it cannot reach. Point it at
 * whatever is already installed instead.
 */
function findChrome () {
  if (process.env.PW_CHROME) return process.env.PW_CHROME
  const base = process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers'
  if (!fs.existsSync(base)) return undefined
  const candidates = fs.readdirSync(base)
    .filter(d => d.startsWith('chromium') && !d.includes('headless_shell'))
    .sort()
    .reverse()
    .map(d => path.join(base, d, 'chrome-linux', 'chrome'))
  candidates.push(path.join(base, 'chromium'))
  return candidates.find(p => fs.existsSync(p) && fs.statSync(p).isFile())
}

const slug = route => (route === '/' ? 'root' : route.replace(/^\//, '').replace(/[^a-zA-Z0-9]+/g, '_'))

/**
 * Sign in through the UI and bank the session, mirroring
 * tools/agent/login_and_capture.mjs. Doing it through the UI rather than by
 * forging a token means a broken sign-in page fails here loudly instead of
 * quietly producing a sweep of redirect pages.
 */
async function login (browser, opts, statePath) {
  const email = process.env.BOW_EMAIL || 'admin@example.com'
  const password = process.env.BOW_PASSWORD || 'Password123!'
  const context = await browser.newContext({ viewport: opts.viewportSize })
  const page = await context.newPage()

  await page.goto(`${opts.baseUrl}/users/sign-in`, { waitUntil: 'load', timeout: opts.timeout })
  // The form is behind a spinner until /api/settings resolves, so a timeout here
  // usually means the backend is down rather than the sign-in page being broken.
  try {
    await page.waitForSelector('#email', { state: 'visible', timeout: opts.timeout })
  } catch {
    await context.close()
    throw new Error(`no sign-in form at ${opts.baseUrl}/users/sign-in after ${opts.timeout}ms. Is the stack up? tools/agent/boot_stack.sh --status`)
  }
  await page.fill('#email', email)
  await page.fill('#password', password)
  await page.click('button[type=submit]')

  // Wait for the navigation rather than sleeping a fixed interval: on a cold
  // `nuxt dev` the post-login route still has to compile, so a short fixed wait
  // reports a working login as "sign-in failed" and sends you off seeding an
  // org that is already fine.
  const left = await page.waitForURL(u => !u.pathname.includes('/users/sign-in'), { timeout: opts.hydrateTimeout })
    .then(() => true).catch(() => false)

  if (!left) {
    const err = await page.locator('.text-red-500').first().textContent().catch(() => null)
    await context.close()
    throw new Error(
      err
        ? `sign-in rejected for ${email}: ${err.trim()}`
        : `sign-in for ${email} did not navigate within ${opts.hydrateTimeout}ms. If the credentials are right, the dev server may still be compiling — retry, or raise --hydrate-timeout. Seed with: cd backend && uv run python ../tools/agent/seed_org.py --demo`
    )
  }
  await page.waitForTimeout(1500)

  // A fresh org lands on the onboarding wizard, which gates every other page.
  // Leaving it undismissed makes the whole sweep look like a redirect bug.
  if (page.url().includes('/onboarding')) {
    const skip = page.getByText('Skip onboarding', { exact: false }).first()
    await skip.click({ timeout: 8000 }).catch(() => {})
    await page.waitForTimeout(2000)
  }
  if (page.url().includes('/onboarding')) {
    console.warn('warning: still on /onboarding after skip — routes will redirect and findings will be noise')
  }

  await context.storageState({ path: statePath })
  await context.close()
  console.log(`signed in as ${email}, session cached at ${statePath}`)
  return statePath
}

/**
 * Runs in the page. Collects interactive controls in one DOM pass — far cheaper
 * than round-tripping per element, and it keeps the snapshot internally
 * consistent (a Vue re-render mid-enumeration would otherwise mix two states).
 */
function collectControls () {
  const SELECTOR = [
    'button',
    'a[href]',
    'input:not([type="hidden"])',
    'select',
    'textarea',
    '[contenteditable="true"]',
    '[role="button"]',
    '[role="link"]',
    '[role="tab"]',
    '[role="menuitem"]',
    '[role="switch"]',
    '[role="checkbox"]'
  ].join(',')

  const norm = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120)

  // Approximates the accessible name: the text a user would call this control.
  const accessibleName = el => {
    const aria = el.getAttribute('aria-label')
    if (aria) return norm(aria)
    const labelledby = el.getAttribute('aria-labelledby')
    if (labelledby) {
      const parts = labelledby.split(/\s+/)
        .map(id => document.getElementById(id)?.innerText)
        .filter(Boolean)
      if (parts.length) return norm(parts.join(' '))
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`)
      if (label) return norm(label.innerText)
    }
    const text = norm(el.innerText || el.textContent)
    if (text) return text
    return norm(el.getAttribute('placeholder') || el.getAttribute('title') || el.value || '')
  }

  // Prefer a selector that survives a re-render: testid > id > aria-label > own text.
  //
  // Order matters more than it looks. `:has-text()` matches *rendered text*, so
  // building it from the accessible name is wrong whenever that name came from
  // an aria-label, a <label for>, or a placeholder — the selector then matches
  // nothing, Phase 3 clicks nothing, and the audit reports a no-op bug that does
  // not exist. Only ever build has-text from the element's own visible text.
  const stableSelector = el => {
    const esc = s => s.replace(/"/g, '\\"')
    const testid = el.getAttribute('data-testid')
    if (testid) return `[data-testid="${testid}"]`
    // Headless UI ids are generated per render (`headlessui-popover-button-v-0-1`)
    // and renumber when anything above them changes, so they look stable and are
    // not. Fall through to name-based selection instead.
    if (el.id && !/^headlessui-/.test(el.id)) return `#${el.id}`

    const tag = el.tagName.toLowerCase()
    const aria = el.getAttribute('aria-label')
    if (aria) return `${tag}[aria-label="${esc(aria)}"]`

    const ownText = norm(el.innerText)
    if (ownText && ownText.length < 40) {
      if (tag === 'button' || tag === 'a') return `${tag}:has-text("${esc(ownText)}")`
      const role = el.getAttribute('role')
      if (role) return `[role="${role}"]:has-text("${esc(ownText)}")`
    }
    if (el.getAttribute('contenteditable') === 'true') return '[contenteditable="true"]'
    return null // Phase 3 will need to derive one by position or context.
  }

  const controls = []
  const seen = new Set()
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (seen.has(el)) continue
    seen.add(el)
    const rect = el.getBoundingClientRect()
    const style = getComputedStyle(el)
    const visible = rect.width > 0 && rect.height > 0 &&
      style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0'
    const type = el.getAttribute('type')
    controls.push({
      tag: el.tagName.toLowerCase(),
      type: type || undefined,
      role: el.getAttribute('role') || undefined,
      name: accessibleName(el),
      selector: stableSelector(el),
      testid: el.getAttribute('data-testid') || undefined,
      href: el.getAttribute('href') || undefined,
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      visible,
      // Destructive-looking controls get clicked last in Phase 3; flagging them
      // here means the auditor sees the risk before planning the click order.
      destructive: /\b(delete|remove|revoke|reset|disconnect|uninstall|clear|destroy|archive)\b/i
        .test(accessibleName(el))
    })
  }
  return {
    controls,
    title: document.title,
    // A page whose body is nearly empty after load is usually a silent render failure.
    bodyTextLength: (document.body?.innerText || '').trim().length
  }
}

async function sweepRoute (context, route, opts) {
  const page = await context.newPage()
  const consoleErrors = []
  const pageErrors = []
  const apiFailures = []
  const apiCalls = []

  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text().slice(0, 500))
  })
  page.on('pageerror', err => pageErrors.push(String(err.message || err).slice(0, 500)))
  page.on('response', res => {
    const url = res.url()
    if (!url.includes('/api/')) return
    const entry = { url: url.replace(opts.baseUrl, ''), status: res.status(), method: res.request().method() }
    apiCalls.push(entry)
    if (res.status() >= 400) apiFailures.push(entry)
  })

  const result = { route, requestedUrl: opts.baseUrl + route }
  try {
    // 'load' rather than 'networkidle': this app holds long-lived polling and
    // websocket connections, so networkidle can never resolve.
    await page.goto(opts.baseUrl + route, { waitUntil: 'load', timeout: opts.timeout })
    // The app is client-rendered: `load` fires on an empty SSR shell. Waiting a
    // fixed interval and reading whatever is there reports a blank page with no
    // console errors — indistinguishable from a genuinely broken route. Wait for
    // hydration to put something on the page, and record when it never arrives
    // so that reads as its own finding rather than "zero controls".
    result.hydrated = await page.waitForFunction(
      () => document.body && document.body.innerText.trim().length > 0,
      { timeout: opts.hydrateTimeout }
    ).then(() => true).catch(() => false)
    await page.waitForTimeout(opts.settle)

    const finalUrl = page.url()
    const finalPath = new URL(finalUrl).pathname
    result.finalUrl = finalUrl
    result.redirected = finalPath.replace(/\/$/, '') !== route.replace(/\/$/, '')

    const collected = await page.evaluate(collectControls)
    Object.assign(result, collected)

    const shotPath = path.join(opts.out, 'screenshots', `${slug(route)}.png`)
    await page.screenshot({ path: shotPath, fullPage: true })
    result.screenshot = path.relative(opts.out, shotPath)
    result.ok = true
  } catch (err) {
    result.ok = false
    result.error = String(err.message || err).split('\n')[0]
  }

  result.consoleErrors = consoleErrors
  result.pageErrors = pageErrors
  result.apiFailures = apiFailures
  result.apiCallCount = apiCalls.length
  await page.close()
  return result
}

async function main () {
  const opts = parseArgs(process.argv)
  const routes = resolveRoutes(opts.routes)
  opts.out = path.resolve(opts.out)
  fs.mkdirSync(path.join(opts.out, 'routes'), { recursive: true })
  fs.mkdirSync(path.join(opts.out, 'screenshots'), { recursive: true })

  if (opts.login && opts.storage) {
    throw new Error('--login and --storage are mutually exclusive: pick one session source.')
  }

  const [width, height] = opts.viewport.split('x').map(Number)
  opts.viewportSize = { width, height }
  const chromium = loadChromium()
  const executablePath = findChrome()
  const browser = await chromium.launch({ headless: !opts.headed, executablePath })

  let storage
  if (opts.login) {
    const statePath = path.join(opts.out, 'state.json')
    storage = (!opts.freshLogin && fs.existsSync(statePath))
      ? statePath
      : await login(browser, opts, statePath)
  } else if (opts.storage === undefined) {
    // No session named. Fall back to the Playwright suite's admin state if it
    // happens to exist, otherwise say so rather than silently sweeping logged
    // out — a logged-out sweep of app routes is 100% redirects and 0% signal.
    const fallback = path.join(repoRoot, 'frontend', 'tests', 'config', 'admin.json')
    if (fs.existsSync(fallback)) {
      storage = fallback
    } else {
      await browser.close()
      throw new Error('No session source. Pass --login (after tools/agent/seed_org.py), --storage <state.json>, or --storage none for the logged-out auth pages.')
    }
  } else {
    storage = opts.storage
    if (storage !== 'none' && !fs.existsSync(storage)) {
      await browser.close()
      throw new Error(`storageState not found: ${storage}`)
    }
  }

  const context = await browser.newContext({
    viewport: { width, height },
    storageState: storage === 'none' ? undefined : storage
  })

  console.log(`Sweeping ${routes.length} route(s) against ${opts.baseUrl}`)
  console.log(`Session: ${storage === 'none' ? 'logged out' : storage}\n`)

  const summary = []
  for (const route of routes) {
    const result = await sweepRoute(context, route, opts)
    fs.writeFileSync(
      path.join(opts.out, 'routes', `${slug(route)}.json`),
      JSON.stringify(result, null, 2)
    )

    // Flag the three things worth acting on before any clicking happens.
    const flags = []
    if (!result.ok) flags.push('LOAD FAILED')
    if (result.redirected) flags.push(`REDIRECTED -> ${result.finalUrl && new URL(result.finalUrl).pathname}`)
    if (result.pageErrors?.length) flags.push(`${result.pageErrors.length} uncaught`)
    if (result.consoleErrors?.length) flags.push(`${result.consoleErrors.length} console err`)
    if (result.apiFailures?.length) flags.push(`${result.apiFailures.length} api fail`)
    if (result.ok && result.hydrated === false) flags.push(`NEVER HYDRATED (${opts.hydrateTimeout}ms)`)
    else if (result.ok && (result.bodyTextLength ?? 0) < 50) flags.push('NEARLY EMPTY')

    const n = result.controls?.length ?? 0
    const visible = result.controls?.filter(c => c.visible).length ?? 0
    console.log(
      `${flags.length ? '!' : ' '} ${route.padEnd(34)} ${String(visible).padStart(3)}/${String(n).padEnd(3)} controls` +
      (flags.length ? `  [${flags.join(', ')}]` : '')
    )

    summary.push({
      route,
      ok: result.ok,
      redirected: result.redirected,
      finalUrl: result.finalUrl,
      controlsTotal: n,
      controlsVisible: visible,
      destructiveControls: result.controls?.filter(c => c.destructive).length ?? 0,
      consoleErrors: result.consoleErrors.length,
      pageErrors: result.pageErrors.length,
      apiFailures: result.apiFailures.length,
      flags
    })
  }

  fs.writeFileSync(path.join(opts.out, 'summary.json'), JSON.stringify(summary, null, 2))
  await browser.close()

  const flagged = summary.filter(s => s.flags.length)
  console.log(`\nWrote ${opts.out}`)
  console.log(`${flagged.length}/${summary.length} route(s) flagged before any interaction.`)
  if (flagged.length) {
    console.log('These are Phase 1 findings — write them up before starting Phase 2.')
  }
}

main().catch(err => {
  console.error(`\nsweep failed: ${err.message}`)
  process.exit(1)
})
