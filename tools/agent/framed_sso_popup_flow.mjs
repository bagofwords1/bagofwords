// Browser check for SSO from a framed BOW (frontend/utils/ssoPopup.ts) in the
// topology where the two windows share no browser channel:
//
//   * the embedding page is on a DIFFERENT SITE from BOW, so the framed BOW
//     page and the top-level BOW popup are in different storage partitions
//     (no shared BroadcastChannel, localStorage or sessionStorage);
//   * the identity provider's page sends `Cross-Origin-Opener-Policy:
//     same-origin`, which severs the popup's `window.opener`.
//
// Neither the embedding site nor the provider is real: both are answered by
// request interception, so the run needs no second host and no provider
// credentials. The provider "succeeds" by sending the popup back to BOW with
// a real single-use login code (tools/agent/mint_login_code.py).
//
//   source tools/agent/devbox_env.sh
//   cd frontend && BOW_URL=https://<bow host> node ../tools/agent/framed_sso_popup_flow.mjs
//
// BOW_URL must be https (COOP only applies to secure pages, and an https
// embedding page cannot frame an http one) and must have exactly one enabled
// SSO provider button, or name it with SSO_BUTTON (a regex on its label).
// Exits non-zero on the first failed expectation.
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(join(process.cwd(), 'package.json'))
const { chromium } = require('playwright')

const BOW = (process.env.BOW_URL || '').replace(/\/+$/, '')
if (!/^https:\/\//.test(BOW)) { console.error('BOW_URL must be an https URL'); process.exit(2) }
const HOST = 'https://embedding-host.test'
const SSO_BUTTON = new RegExp(process.env.SSO_BUTTON || 'Sign in with', 'i')
const OUT = process.env.OUT || ''
const here = dirname(fileURLToPath(import.meta.url))
const mint = () => execFileSync('python3', [join(here, 'mint_login_code.py')], { encoding: 'utf8' }).trim()

let failures = 0
function expect(ok, what) {
  console.log(`${ok ? 'PASS' : 'FAIL'}: ${what}`)
  if (!ok) failures++
}
const bowFrame = (page) => page.frames().find((f) => f !== page.mainFrame() && f.url().startsWith(BOW))

// One attempt: open the embedding page, click the SSO button in the framed
// BOW, and let the popup reach the (fake, opener-severing) provider.
async function start(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 860 } })
  await context.route(`${HOST}/**`, (route) => route.fulfill({
    contentType: 'text/html',
    body: `<!doctype html><title>Embedding host</title><body style="margin:0;font-family:sans-serif">
      <p style="margin:12px">Embedding host on another site</p>
      <iframe src="${BOW}/" style="width:100%;height:760px;border:2px solid #333"></iframe>`,
  }))
  // Everything the popup loads that is neither BOW nor the embedding page is
  // "the provider": a page that isolates itself, as a provider's may.
  await context.route((url) => url.origin !== BOW && url.origin !== HOST, (route) => {
    if (!route.request().isNavigationRequest()) return route.abort()
    return route.fulfill({
      contentType: 'text/html',
      headers: { 'Cross-Origin-Opener-Policy': 'same-origin' },
      body: '<!doctype html><title>Fake provider</title><h1>Provider sign-in</h1>',
    })
  })
  const page = await context.newPage()
  const calls = []
  context.on('response', (res) => {
    const path = new URL(res.url()).pathname
    if (!res.url().startsWith(BOW) || !path.startsWith('/api/auth/exchange')) return
    calls.push({ path, method: res.request().method(), status: res.status(), fromFrame: res.request().frame() === bowFrame(page) })
  })
  // Not 'networkidle': a dev server streams hundreds of unbundled modules and
  // keeps prefetching, so the network may never go quiet. The button is the
  // signal that the framed sign-in page is ready.
  await page.goto(`${HOST}/`, { waitUntil: 'load' })
  await page.waitForFunction((bow) => [...document.querySelectorAll('iframe')].some((f) => f.src.startsWith(bow)), BOW)
  let frame
  for (let i = 0; i < 100 && !(frame = bowFrame(page)); i++) await page.waitForTimeout(100)
  const button = frame.getByRole('button', { name: SSO_BUTTON }).first()
  await button.waitFor({ timeout: 120000 })
  const [popup] = await Promise.all([page.waitForEvent('popup'), button.click()])
  await popup.waitForURL((url) => url.origin !== BOW, { timeout: 30000 })
  await popup.waitForLoadState('domcontentloaded')
  return { context, page, popup, frame, calls }
}

// Chrome partitions third-party storage; Playwright switches that off for its
// browsers (`--disable-features=…,ThirdPartyStoragePartitioning`). Put it back,
// or this run would not be in the topology it claims to test. The check below
// (the frame cannot read what the popup stored) fails loudly if it did not work.
function launchOptions() {
  try {
    const switches = require(join(dirname(require.resolve('playwright-core/package.json')), 'lib/server/chromium/chromiumSwitches.js'))
    const disable = switches.chromiumSwitches().find((arg) => arg.startsWith('--disable-features='))
    if (!disable) return {}
    const kept = disable.slice('--disable-features='.length).split(',').filter((f) => f !== 'ThirdPartyStoragePartitioning')
    return { ignoreDefaultArgs: [disable], args: [`--disable-features=${kept.join(',')}`] }
  } catch { return {} }
}
const browser = await chromium.launch(launchOptions())

// ── 1. the provider signs the user in ──────────────────────────────────────
{
  const { context, page, popup, calls } = await start(browser)
  expect(await popup.title() === 'Fake provider', 'popup reached the provider page (top-level, not framed)')

  const closed = popup.waitForEvent('close', { timeout: 60000 }).then(() => true, () => false)
  const landed = popup.waitForEvent('domcontentloaded')
  await popup.evaluate((url) => { window.location.href = url }, `${BOW}/users/sign-in?login_code=${mint()}`)
  await landed
  // The two properties that make this the hard case.
  const severed = await popup.evaluate(() => {
    try { localStorage.setItem('bow:test:partition-probe', '1') } catch { /* storage off */ }
    return window.opener === null
  }).catch(() => null)
  const frameSees = await bowFrame(page).evaluate(() => {
    try { return localStorage.getItem('bow:test:partition-probe') } catch { return 'unreadable' }
  })
  expect(severed === true, 'the provider page severed the popup\'s opener (window.opener === null)')
  expect(frameSees === null, `frame and popup are in different storage partitions (frame read: ${JSON.stringify(frameSees)})`)

  expect(await closed, 'popup closed itself')
  const handoff = calls.find((c) => c.method === 'POST' && c.path === '/api/auth/exchange/handoff')
  const exchangeAt = calls.findIndex((c) => c.method === 'POST' && c.path === '/api/auth/exchange')
  const exchange = calls[exchangeAt]
  expect(handoff?.status === 204 && !handoff.fromFrame, 'popup handed its login code off to the server (204)')
  expect(exchange?.status === 200, 'the frame exchanged the handed-off session (POST /api/auth/exchange 200)')
  expect(exchange?.fromFrame === true, 'the exchange came from the framed page, not the popup')
  const consumedSeen = calls.slice(exchangeAt + 1).some((c) => c.method === 'GET' && c.status === 200)
  expect(exchangeAt >= 0 && consumedSeen, 'popup closed only after asking the server once the frame had exchanged')
  if (OUT) await page.screenshot({ path: join(OUT, 'cross-site-after-exchange.png') })
  await context.close()
}

// ── 2. the provider refuses ────────────────────────────────────────────────
// No session exists to hand over and no channel reaches the frame, so the
// popup must not close on a result nobody received: it stays and says why.
{
  const { context, popup } = await start(browser)
  const landed = popup.waitForEvent('domcontentloaded')
  await popup.evaluate((url) => { window.location.href = url }, `${BOW}/users/sign-in?error_code=oauth_login_failed`)
  await landed
  const alert = popup.getByText(/sign-in failed/i).first()
  const shown = await alert.waitFor({ timeout: 20000 }).then(() => true, () => false)
  expect(!popup.isClosed(), 'popup stayed open when nothing acknowledged the failure')
  expect(shown, 'popup shows the provider failure itself')
  if (OUT && !popup.isClosed()) await popup.screenshot({ path: join(OUT, 'cross-site-error-in-popup.png') })
  await context.close()
}

// ── 3. the frame is gone when the provider returns ─────────────────────────
// The session is handed off but nobody takes it. The popup must not read the
// handoff running out as the frame having taken it: it stays open and says so.
{
  const { context, page, popup } = await start(browser)
  await page.evaluate(() => document.querySelector('iframe')?.remove())
  const closed = popup.waitForEvent('close', { timeout: 75000 }).then(() => true, () => false)
  const landed = popup.waitForEvent('domcontentloaded')
  await popup.evaluate((url) => { window.location.href = url }, `${BOW}/users/sign-in?login_code=${mint()}`)
  await landed
  const said = popup.getByText(/could not pass it back/i).first()
  const shown = await said.waitFor({ timeout: 75000 }).then(() => true, () => false)
  expect(shown && !popup.isClosed(), 'with the frame gone, the popup stays open and says the sign-in could not be passed back')
  if (OUT && !popup.isClosed()) await popup.screenshot({ path: join(OUT, 'frame-gone-popup-stays.png') })
  await context.close()
  await closed
}

await browser.close()
console.log(failures ? `\n${failures} expectation(s) failed` : '\nall expectations passed')
process.exit(failures ? 1 : 0)
