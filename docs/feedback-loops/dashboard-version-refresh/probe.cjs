// Run against a local Nuxt frontend. All API responses are local fixtures.
const { chromium } = require(require.resolve('playwright', { paths: [require('node:path').resolve(__dirname, '../../../frontend')] }))
const assert = require('node:assert/strict')
const path = require('node:path')
const origin = process.env.ORIGIN || 'http://127.0.0.1:3318'
const output = process.env.EVIDENCE_DIR
const label = process.env.BASELINE ? 'before' : 'after'
const report = { id: 'refresh-test', title: 'Version refresh verification', user_id: 'user', artifact_count: 1, data_sources: [], files: [] }
function version(n) {
  return {
    id: 'version-' + n, artifact_id: 'dashboard', report_id: report.id,
    title: 'Version refresh verification', mode: 'page', status: 'completed', version: n,
    created_at: '2026-11-01T00:00:00Z',
    content: { visualization_ids: [], code: `<script type="text/babel">
      function App() {
        const [count, setCount] = React.useState(0);
        return <main style={{padding:32}}><h1>Dashboard v${n}</h1>
          <button onClick={()=>setCount(count+1)}>Counter {count}</button></main>;
      }
      ReactDOM.render(<App/>, document.getElementById('root'));
    </script>` },
  }
}

async function run() {
  const browser = await chromium.launch()
  try {
    const viewport = { width: 1440, height: 900 }
    const page = await browser.newPage({
      viewport,
      recordVideo: process.env.VIDEO_DIR ? { dir: process.env.VIDEO_DIR, size: viewport } : undefined,
    })
    page.on('pageerror', error => console.error(error.message))
    page.setDefaultTimeout(60000)
    const v7 = version(7), v8 = version(8)
    let latest = v7
    let extras = []
    await page.context().addCookies([{ name: 'auth.token', value: 'local-fixture', url: origin }])
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== origin) return route.abort()
      const endpoint = url.pathname.replace(/^\/api\/api\//, '/api/')
      if (!endpoint.startsWith('/api/')) return route.continue()
      let body = []
      if (endpoint.endsWith('/whoami')) body = { id: 'user', is_verified: true, organizations: [{ id: 'org', name: 'Local workspace', role: 'member', permissions: [] }] }
      else if (endpoint.endsWith('/reports/refresh-test')) body = report
      else if (endpoint.endsWith('/artifacts/report/refresh-test')) body = [...extras, ...(latest === v7 ? [v7] : [v8, v7])]
      else if (endpoint.endsWith('/latest')) body = latest
      else if (endpoint.endsWith('/artifacts/version-7')) body = v7
      else if (endpoint.endsWith('/artifacts/version-8')) body = v8
      else if (endpoint.endsWith('/history')) body = { versions: [latest].map(v => ({ id: v.id, number: v.version, report_id: report.id })) }
      else if (endpoint.endsWith('/organization/locale')) body = { effective_locale: 'en' }
      else if (/settings|context|draft|open-state/.test(endpoint)) body = {}
      await route.fulfill({ json: body })
    })
    let loads = 0
    await page.exposeFunction('recordDashboardLoad', () => { loads++ })
    await page.addInitScript(() => document.addEventListener('load', event => {
      if (event.target.matches?.('iframe[data-artifact-frame]')) window.recordDashboardLoad()
    }, true))
    await page.goto(origin + '/reports/' + report.id, { waitUntil: 'domcontentloaded' })
    const frame = page.frameLocator('iframe[data-artifact-frame]:visible')
    const counter = frame.getByRole('button')
    await counter.click()
    await frame.getByText('Counter 1').waitFor()
    const initial = loads
    const observations = []
    for (const [index, v] of [v7, v8, v8].entries()) {
      latest = v
      // Default to the 15-second timer. FOCUS=1 exercises its on-focus shortcut.
      const history = page.waitForResponse(response => response.url().endsWith('/dashboard/history'))
      if (process.env.FOCUS) await page.evaluate(() => window.dispatchEvent(new Event('focus')))
      await history
      if (!process.env.BASELINE && index === 1) await frame.getByRole('heading', { name: 'Dashboard v8' }).waitFor()
      await page.waitForTimeout(750) // includes the iframe's delayed ready message
      const row = [await frame.getByRole('heading').innerText(), await counter.innerText(), loads - initial]
      observations.push(row)
      if (output && index === 1) await page.screenshot({ path: path.join(output, label + '.png') })
      if (index === 1 && !process.env.BASELINE) await counter.click()
    }
    console.log(JSON.stringify(observations))
    if (!process.env.BASELINE) {
      extras = [{ ...version(9), status: 'pending' }, { ...version(12), artifact_id: 'other-dashboard' }]
      const polled = page.waitForResponse(response => response.url().endsWith('/dashboard/history'))
      await page.evaluate(() => window.dispatchEvent(new Event('focus')))
      await polled
      const repeated = page.waitForResponse(response => response.url().endsWith('/artifacts/report/refresh-test'))
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('artifact:created', {
        detail: { report_id: 'refresh-test', artifact_id: 'version-8' },
      })))
      await repeated
      await page.waitForTimeout(750)
      assert.equal(await counter.innerText(), 'Counter 1')
      assert.equal(loads - initial, 1)
    }
    if (!process.env.BASELINE) assert.deepEqual(observations, [
      ['Dashboard v7', 'Counter 1', 0], ['Dashboard v8', 'Counter 0', 1], ['Dashboard v8', 'Counter 1', 1],
    ])
    await page.context().close()
  } finally { await browser.close() }
}
run().catch(error => { console.error(error); process.exitCode = 1 })
