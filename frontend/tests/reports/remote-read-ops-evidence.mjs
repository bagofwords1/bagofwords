// Remote-server (SSH/SMB) read operations render as file reads, not raw
// "read_file · server" rows under a terminal icon. Seeded full-stack report,
// production components, deterministic tool records.
// BOW_TEST_EMAIL=... BOW_TEST_PASSWORD=... node tests/reports/remote-read-ops-evidence.mjs before|after
import { chromium } from '@playwright/test'
import { mkdirSync, writeFileSync } from 'node:fs'
import assert from 'node:assert/strict'
const phase = process.argv[2] || 'after'
const base = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000'
const api = process.env.BOW_BASE_URL || 'http://localhost:8000'
const out = process.env.SHOT_DIR || '../media/pr/remote-read-ops'
const email = process.env.BOW_TEST_EMAIL, password = process.env.BOW_TEST_PASSWORD
assert(email && password, 'Provide throwaway credentials')
mkdirSync(out, { recursive: true })
const login = await fetch(`${api}/api/auth/jwt/login`, { method: 'POST', body: new URLSearchParams({ username: email, password }) })
assert(login.ok)
const { access_token } = await login.json()
const auth = { Authorization: `Bearer ${access_token}` }
const orgs = await (await fetch(`${api}/api/organizations`, { headers: auth })).json()
const reportResponse = await fetch(`${api}/api/reports`, { method: 'POST', headers: { ...auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' }, body: JSON.stringify({ title: 'Server log review', data_sources: [] }) })
assert(reportResponse.ok)
const report = await reportResponse.json()

const target = { server: 'web-01.example', port: 22 }
const op = (id, tool_name, args, result = {}, status = 'success', duration_ms = 800) => ({
  id, block_index: 0, status: status === 'running' ? 'in_progress' : 'completed',
  tool_execution: {
    id, tool_name: 'execute_mcp', status, duration_ms,
    arguments_json: { connection_id: 'fixture-ssh', tool_name, arguments: args },
    result_json: { success: status !== 'error', connection_name: 'Demo server', connection_type: 'remote_ssh', execution_target: target, ...result },
  },
})
const blocks = [
  op('read', 'read_file', { path: '/var/log/nginx/error.log', limit: 200, tail: true }, { preview: '2026/11/19 09:12:03 [error] upstream timed out' }),
  op('list', 'list_directory', { path: '/var/log/nginx', limit: 50 }, { preview: 'access.log\nerror.log' }),
  op('search', 'search_file', { path: '/var/log/app/app.log', limit: 50, text: 'timeout' }, { preview: '12: request timeout after 30s' }),
  op('status', 'system_status', {}, { preview: 'load 0.42, disk 61%' }),
  op('failed', 'read_file', { path: '/var/log/secure' }, { error_message: 'Path is outside the configured roots' }, 'error'),
  op('shell', 'execute_shell', { command: 'systemctl status nginx', working_directory: '/srv' }, { preview: { stdout: 'active (running)', stderr: '', exit_code: 0 } }),
  op('running', 'read_file', { path: '/var/log/syslog', limit: 200 }, {}, 'running'),
].map((b, i) => ({ ...b, block_index: i }))

const browser = await chromium.launch()
const results = []
try {
  for (const locale of ['en', 'he']) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
    await context.addInitScript(l => localStorage.setItem('bow.locale', l), locale)
    const page = await context.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))
    await page.goto(`${base}/users/sign-in`, { waitUntil: 'domcontentloaded', timeout: 240000 })
    await page.locator('#email').fill(email)
    await page.locator('#password').fill(password)
    await page.locator('button[type=submit]').click()
    await page.waitForURL(u => !u.pathname.includes('sign-in'), { timeout: 240000 })
    await page.goto(`${base}/reports/${report.id}`, { waitUntil: 'domcontentloaded', timeout: 240000 })
    await page.locator('.mention-input-field').first().waitFor({ timeout: 240000 })
    await page.waitForFunction(() => {
      for (const el of document.querySelectorAll('*')) {
        let c = el.__vueParentComponent
        while (c) { if (c.setupState?.messages && c.setupState?.handleStreamingEvent) { window.rep = c.setupState; return true } c = c.parent }
      }
      return false
    })
    await page.evaluate(({ locale, blocks }) => {
      const s = window.rep
      s.stopWatchStream(); s.stopPollingInProgressCompletion(); s.isStreaming = false; s.isSplitScreen = false
      s.report.data_sources = [{ id: 'fixture-agent', name: 'Operations', connections: [{ id: 'fixture-ssh', name: 'Demo server', type: 'remote_ssh' }] }]
      s.messages = [
        { id: 'fixture-user', role: 'user', status: 'success', prompt: { content: locale === 'he' ? 'למה nginx מחזיר שגיאות 504?' : 'Why is nginx returning 504s?' } },
        { id: 'fixture-system', role: 'system', status: 'in_progress', model_name: 'Claude Sonnet', prompt: { response_locale: locale }, completion_blocks: blocks },
      ]
    }, { locale, blocks })
    const chat = page.locator('.chat-messages').first()
    const header = chat.getByTestId('block-group-header').first()
    await header.waitFor()
    await page.waitForTimeout(800)
    const groupLabel = await header.innerText()
    await chat.screenshot({ path: `${out}/${phase}-group-${locale}.png` })
    await header.click()
    await page.locator('.tool-execution-container').first().waitFor()
    await page.waitForTimeout(500)
    await chat.screenshot({ path: `${out}/${phase}-${locale}.png` })
    // Labels wrap LTR fragments in invisible bidi isolates (U+2068/U+2069).
    const text = (await chat.innerText()).replace(/[\u2066-\u2069]/g, '')
    if (phase === 'after') {
      const en = locale === 'en'
      // The collapsed group names the running operation, not "Calling a tool".
      assert(groupLabel.includes('Reading a file'), `group label: ${groupLabel}`)
      for (const s of en
        ? ['Read error.log · web-01.example', 'Listed nginx · web-01.example', 'Searched app.log for “timeout” · web-01.example', 'Checked server status · web-01.example', 'Reading syslog… · web-01.example', 'Demo server (failed)']
        : ['נקרא error.log · web-01.example', 'הוצג תוכן nginx · web-01.example', 'מצב השרת נבדק · web-01.example']) {
        assert(text.includes(s), `missing label: ${s}`)
      }
      assert(!/read_file|list_directory|search_file|system_status/.test(text), 'raw operation names hidden')
      // Finished read ops (the failed read too) get an operation icon; the
      // shell keeps its terminal and the running row its spinner.
      assert.equal(await page.getByTestId('remote-op-label').count(), 5)
      assert.equal(await page.getByTestId('remote-shell-tool').count(), 1, 'shell keeps its own card')
      assert.equal(await chat.locator('[title="/var/log/nginx/error.log"]').count(), 1, 'full path in tooltip')
      // Expanded body still shows the input and output.
      await chat.getByText(en ? /^Read\W+error\.log/ : /^נקרא\W+error\.log/).click()
      await chat.getByText('upstream timed out', { exact: false }).waitFor()
      await chat.screenshot({ path: `${out}/expanded-${locale}.png` })
    }
    results.push({ locale, errors })
    await context.close()
    assert.deepEqual(errors, [], 'no browser errors')
  }
  writeFileSync(`${out}/${phase}.json`, JSON.stringify(results, null, 2))
  console.log(JSON.stringify({ phase, results }))
} finally { await browser.close() }
