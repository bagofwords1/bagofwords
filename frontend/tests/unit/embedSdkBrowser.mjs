import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { chromium } from 'playwright'

// Run: node --test tests/unit/embedSdkBrowser.mjs (needs Playwright's Chromium)
// The host's SDK (backend/app/static/embed/bow-embed.js) in a real browser:
// what embedSdk.mjs can only stub — which size and colour values the browser
// keeps, the vh line kept under the dvh one, and the launcher's computed colour.

const sdk = readFileSync(new URL('../../../backend/app/static/embed/bow-embed.js', import.meta.url), 'utf8')
const BOW = 'https://bow.example.com'

const browser = await chromium.launch()
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  // The frame's BOW page is never fetched: every request gets an empty answer.
  await page.route('**/*', (route) => route.fulfill({ status: 200, contentType: 'text/html', body: '' }))
  const warnings = []
  page.on('console', (msg) => { if (msg.type() === 'warning') warnings.push(msg.text()) })

  const mount = async (options, viewport = { width: 1280, height: 900 }) => {
    await page.setViewportSize(viewport)
    await page.setContent('<style>:root{--brand:#7c3aed}</style><div id="bow" style="height:500px"></div>')
    await page.addScriptTag({ content: sdk })
    warnings.length = 0
    return page.evaluate(({ options, BOW }) => {
      const embed = window.BOWEmbed.mount({ baseUrl: BOW, hostApp: 'host_1', artifact: 'a1', startTimeout: 0, ...options })
      const launcher = document.querySelector('[data-bow-embed-launcher]')
      if (launcher) launcher.click()
      const box = embed.iframe.parentNode.getBoundingClientRect()
      const panel = getComputedStyle(embed.iframe.parentNode)
      return {
        width: Math.round(box.width),
        height: Math.round(box.height),
        zIndex: panel.zIndex,
        accent: launcher ? getComputedStyle(launcher).backgroundColor : null,
      }
    }, { options, BOW })
  }
  const floating = (options = {}, viewport) => mount({ mode: 'floating', ...options }, viewport)

  // Floating: 600 x 720 by default; host sizes, including calc() and dvh, are kept.
  assert.deepEqual(await floating().then(({ width, height }) => [width, height]), [600, 720])
  assert.deepEqual(await floating({ width: 480, height: '40rem' }).then(({ width, height }) => [width, height]), [480, 640])
  assert.equal((await floating({ height: 'calc(100vh - 300px)' })).height, 600)
  assert.equal((await floating({ height: '50dvh' })).height, 450)
  assert.deepEqual(warnings, [])
  // The viewport caps both sides.
  assert.deepEqual(await floating({}, { width: 390, height: 700 }).then(({ width, height }) => [width, height]), [358, 580])
  // A value the browser refuses warns and is the default.
  assert.equal((await floating({ width: '600px;background:red' })).width, 600)
  assert.equal(warnings.length, 1)
  assert.equal((await floating({ height: '10' })).height, 720)
  assert.equal(warnings.length, 1)

  // Inline: a number is pixels, a CSS height is kept, a refused one is 100% of the container.
  assert.equal((await mount({ container: '#bow', height: 320 })).height, 320)
  assert.equal((await mount({ container: '#bow', height: 'calc(100px + 20px)' })).height, 120)
  assert.equal((await mount({ container: '#bow', height: 'nonsense' })).height, 500)
  assert.equal(warnings.length, 1)

  // z-index 'auto' is kept.
  assert.equal((await floating({ zIndex: 'auto' })).zIndex, 'auto')

  // accentColor: kept when visible; see-through or white falls back to the default.
  assert.equal((await floating({ accentColor: 'var(--brand)' })).accent, 'rgb(124, 58, 237)')
  for (const hidden of ['transparent', 'currentcolor', 'var(--missing)', '#ffffff']) {
    assert.equal((await floating({ accentColor: hidden })).accent, 'rgb(37, 99, 235)', `refuses ${hidden}`)
    assert.equal(warnings.length, 1)
  }
} finally {
  await browser.close()
}
console.log('PASS: in Chromium, sizes, z-index and the launcher colour behave as the SDK says')
