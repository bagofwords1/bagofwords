// One direction per chat reply: a Hebrew answer split by tool calls stays RTL —
// its row, every markdown block, table and list, and its tool cards — whether
// the saved response language is pending, right, or wrong; an English answer
// stays LTR. Runs the real report page against a seeded stack and injects a
// deterministic transcript, then asserts the browser's computed directions.
//
//   tools/agent/boot_stack.sh --dev && (cd backend && uv run python ../tools/agent/seed_org.py)
//   cd frontend && node tests/reports/rtl-message-direction.mjs after
//
// `before` only captures screenshots (run it on the base branch); `after`
// captures and asserts.
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import assert from 'node:assert/strict'

const phase = process.argv[2] || 'after'
const base = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:3000'
const api = process.env.BOW_BASE_URL || 'http://localhost:8000'
const out = process.env.SHOT_DIR || '../media/pr/rtl-message-direction'
const email = process.env.BOW_TEST_EMAIL || 'admin@example.com'
const password = process.env.BOW_TEST_PASSWORD || 'Password123!'
const executablePath = process.env.CHROMIUM_PATH || undefined
mkdirSync(out, { recursive: true })

const login = await fetch(`${api}/api/auth/jwt/login`, { method: 'POST', body: new URLSearchParams({ username: email, password }) })
assert(login.ok, 'login')
const { access_token } = await login.json()
const auth = { Authorization: `Bearer ${access_token}` }
const orgs = await (await fetch(`${api}/api/organizations`, { headers: auth })).json()
const created = await fetch(`${api}/api/reports`, {
  method: 'POST',
  headers: { ...auth, 'X-Organization-Id': orgs[0].id, 'Content-Type': 'application/json' },
  body: JSON.stringify({ title: 'Revenue by customer', data_sources: [] }),
})
assert(created.ok, 'create report')
const report = await created.json()

const SQL_TABLE = [
  '| customer_name | region | revenue |',
  '|---|---|---|',
  '| Acme Corp | North America | 1,200,000 |',
  '| Globex Inc | EMEA West | 980,000 |',
  '| Initech LLC | APAC South | 760,000 |',
].join('\n')
const NAME_LIST = '- **Acme Corp** – Enterprise plan, North America\n- **Globex Inc** – Pro plan, EMEA\n- **Initech LLC** – Starter plan, APAC'
const HE_INTRO = 'בדקתי את טבלת `orders` ואת טבלת הלקוחות, והנה הלקוחות המובילים לפי הכנסות ברבעון האחרון:'
const HE_OUTRO = 'שלושת הלקוחות האלה אחראים לרוב ההכנסות, ו-Acme Corp לבדה מהווה כמעט מחצית מהן.'

const tool = (id, name, title) => ({
  id: `${id}-tool`, block_index: 0, status: 'completed', content: '',
  tool_execution: { id: `${id}-exec`, tool_name: name, status: 'success', duration_ms: 1840, arguments_json: { title }, result_summary: title },
})
const text = (id, content) => ({ id, status: 'completed', content })

// A Hebrew reply split by a tool call into an intro, a data table, a list and
// a closing sentence: the shape that used to come out half LTR.
const splitReply = (prefix) => [
  text(`${prefix}-intro`, HE_INTRO),
  tool(prefix, 'run_customer_query', 'Revenue by customer'),
  text(`${prefix}-table`, SQL_TABLE),
  text(`${prefix}-list`, NAME_LIST),
  text(`${prefix}-outro`, HE_OUTRO),
].map((b, i) => ({ ...b, block_index: i }))

const transcript = [
  // 1. Saved response language not announced yet (or a pre-feature turn).
  { id: 'q-pending', role: 'user', status: 'success', prompt: { content: 'מי הלקוחות המובילים שלנו לפי הכנסות ברבעון האחרון?' } },
  { id: 'a-pending', role: 'system', status: 'success', model: { name: 'BoW' }, prompt: {}, completion_blocks: splitReply('pending') },
  // 2. A wrong saved language: the classifier said English for Hebrew.
  { id: 'q-wrong', role: 'user', status: 'success', prompt: { content: 'תראה לי revenue לפי customer_segment ב-Q3' } },
  { id: 'a-wrong', role: 'system', status: 'success', model: { name: 'BoW' }, prompt: { response_locale: 'en' }, completion_blocks: splitReply('wrong') },
  // 3. Saved Hebrew: must stay RTL everywhere, wrapper and table scroller included.
  { id: 'q-he', role: 'user', status: 'success', prompt: { content: 'תסכם לי את הלקוחות המובילים' } },
  { id: 'a-he', role: 'system', status: 'success', model: { name: 'BoW' }, prompt: { response_locale: 'he' }, completion_blocks: splitReply('he') },
  // Control: an English answer naming a Hebrew customer stays LTR.
  { id: 'q-en', role: 'user', status: 'success', prompt: { content: 'Who were our top customers last quarter?' } },
  { id: 'a-en', role: 'system', status: 'success', model: { name: 'BoW' }, prompt: { response_locale: 'en' }, completion_blocks: [
    text('en-intro', 'Here are the top customers by revenue last quarter, including the Israeli account:'),
    tool('en', 'run_customer_query', 'Revenue by customer'),
    text('en-table', '| Customer | Region | Revenue |\n|---|---|---|\n| חברת אלפא | Israel | 1,100,000 |\n| Acme Corp | North America | 1,200,000 |'),
    text('en-list', '- **חברת אלפא** renewed its enterprise contract\n- **Acme Corp** expanded to two new regions'),
  ].map((b, i) => ({ ...b, block_index: i })) },
]

// Streaming frames: a Hebrew answer arriving before its saved language, with a
// tool call and then an English-data table streamed row by row.
const streamFrames = (() => {
  const frames = []
  const words = HE_INTRO.split(' ')
  for (let i = 1; i <= words.length; i += 3) frames.push([text('s-intro', words.slice(0, i).join(' '))])
  const intro = text('s-intro', HE_INTRO)
  frames.push([intro, { ...tool('s', 'run_customer_query', 'Revenue by customer'), tool_execution: { ...tool('s', 'run_customer_query', 'Revenue by customer').tool_execution, status: 'running' } }])
  const ran = tool('s', 'run_customer_query', 'Revenue by customer')
  const rows = SQL_TABLE.split('\n')
  for (let i = 1; i <= rows.length; i++) frames.push([intro, ran, text('s-table', rows.slice(0, i).join('\n'))])
  const lines = NAME_LIST.split('\n')
  for (let i = 1; i <= lines.length; i++) frames.push([intro, ran, text('s-table', SQL_TABLE), text('s-list', lines.slice(0, i).join('\n'))])
  frames.push([intro, ran, text('s-table', SQL_TABLE), text('s-list', NAME_LIST), text('s-outro', HE_OUTRO)])
  return frames.map((f) => f.map((b, i) => ({ ...b, block_index: i })))
})()

const browser = await chromium.launch({ executablePath })
const failures = []
try {
  for (const locale of ['en', 'he']) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 })
    await context.addInitScript((l) => localStorage.setItem('bow.locale', l), locale)
    const page = await context.newPage()
    const errors = []
    page.on('pageerror', (e) => errors.push(String(e)))
    await page.goto(`${base}/users/sign-in`, { waitUntil: 'domcontentloaded', timeout: 120000 })
    await page.locator('#email').fill(email)
    await page.locator('#password').fill(password)
    await page.locator('button[type=submit]').click()
    await page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 120000 })
    if (page.url().includes('/onboarding')) {
      await page.getByRole('button', { name: locale === 'he' ? /דלג|דילוג/ : 'Skip onboarding' }).click()
      await page.waitForURL((u) => !u.pathname.includes('/onboarding'))
    }
    await page.goto(`${base}/reports/${report.id}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
    await page.locator('.mention-input-field').first().waitFor({ timeout: 120000 })
    await page.waitForFunction(() => {
      for (const el of document.querySelectorAll('*')) {
        let c = el.__vueParentComponent
        while (c) {
          if (c.setupState?.messages && c.setupState?.handleStreamingEvent) { window.rtlReport = c.setupState; return true }
          c = c.parent
        }
      }
      return false
    })
    await page.evaluate((messages) => {
      const s = window.rtlReport
      s.stopWatchStream?.(); s.stopPollingInProgressCompletion?.(); s.isStreaming = false; s.isSplitScreen = false
      s.messages = JSON.parse(JSON.stringify(messages))
    }, transcript)
    await page.locator('[data-message-id="a-en"] table').waitFor()
    // The floating scroll pill would cover the element shots.
    await page.addStyleTag({ content: '[data-testid="jump-to-latest"]{display:none!important}' })
    await page.waitForTimeout(400)

    for (const id of ['a-pending', 'a-wrong', 'a-he', 'a-en']) {
      const li = page.locator(`[data-message-id="${id}"]`)
      await li.scrollIntoViewIfNeeded()
      await li.screenshot({ path: `${out}/${phase}-${locale}-${id}.png` })
    }
    await page.locator('[data-message-id="q-pending"]').scrollIntoViewIfNeeded()
    await page.screenshot({ path: `${out}/${phase}-${locale}-page.png` })

    if (phase === 'after') {
      const dirs = await page.evaluate(() => {
        const cs = (el) => getComputedStyle(el).direction
        const of = (id) => {
          const li = document.querySelector(`[data-message-id="${id}"]`)
          const all = (sel) => [...li.querySelectorAll(sel)].map(cs)
          return {
            row: cs(li.querySelector('.assistant-content')),
            wrappers: all('.markdown-wrapper'),
            scrollers: all('.table-node-wrapper'),
            tables: all('table'),
            cells: all('th, td').filter((d, i, a) => a.indexOf(d) === i),
            lists: all('ul, li').filter((d, i, a) => a.indexOf(d) === i),
            paragraphs: all('.markdown-wrapper p').filter((d, i, a) => a.indexOf(d) === i),
            tools: all('.tool-execution-container'),
          }
        }
        return { pending: of('a-pending'), wrong: of('a-wrong'), he: of('a-he'), en: of('a-en') }
      })
      const expect = { pending: 'rtl', wrong: 'rtl', he: 'rtl', en: 'ltr' }
      for (const [k, want] of Object.entries(expect)) {
        const d = dirs[k]
        for (const [part, got] of Object.entries(d)) {
          const vals = Array.isArray(got) ? got : [got]
          if (!vals.length) { failures.push(`${locale}/${k}: no ${part}`); continue }
          // Signed-number cells are the one LTR exception; none in this fixture.
          if (vals.some((v) => v !== want)) failures.push(`${locale}/${k}: ${part}=${JSON.stringify(vals)} want ${want}`)
        }
      }
      // The Hebrew reply's table opens on its right edge.
      const rightAligned = await page.evaluate(() => {
        const li = document.querySelector('[data-message-id="a-pending"]')
        const th = li.querySelector('th'), table = li.querySelector('table')
        return Math.abs(th.getBoundingClientRect().right - table.getBoundingClientRect().right) < 2
      })
      if (!rightAligned) failures.push(`${locale}: first column of the Hebrew table is not on the right`)
      console.log(locale, JSON.stringify(dirs))
    }
    if (locale === 'en') {
      // Stream the answer in the English UI (the case with no RTL fallback).
      await page.evaluate((q) => {
        const s = window.rtlReport
        s.messages = [q, { id: 'a-stream', role: 'system', status: 'in_progress', model: { name: 'BoW' }, prompt: {}, completion_blocks: [] }]
      }, transcript[0])
      await page.waitForTimeout(500)
      const framesDir = `${out}/frames-${phase}`
      mkdirSync(framesDir, { recursive: true })
      for (const [n, frame] of streamFrames.entries()) {
        await page.evaluate((blocks) => { window.rtlReport.messages[1].completion_blocks = blocks }, frame)
        await page.waitForTimeout(450)
        // Frames for a GIF of the stream (assemble them with ffmpeg or PIL).
        await page.locator('[data-testid="transcript"]').screenshot({ path: `${framesDir}/${String(n).padStart(2, '0')}.png` })
        if (phase === 'after') {
          const bad = await page.evaluate(() => {
            const li = document.querySelector('[data-message-id="a-stream"]')
            if (!li) return ['no reply']
            const sel = '.assistant-content, .markdown-wrapper, .table-node-wrapper, table, th, td, ul, li, .markdown-wrapper p, .tool-execution-container'
            return [...li.querySelectorAll(sel)].filter((el) => getComputedStyle(el).direction !== 'rtl').map((el) => el.tagName + '.' + el.className)
          })
          if (bad.length) failures.push(`stream frame ${n}: LTR ${bad.slice(0, 4).join(', ')}`)
        }
      }
      await page.evaluate(() => { window.rtlReport.messages[1].status = 'success' })
      await page.waitForTimeout(800)
    }
    // The shared-link page renders the same transcript with its own template.
    await page.route('**/api/c/rtl-fixture*', (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ title: 'Revenue by customer', user_name: 'Agent Admin', created_at: new Date().toISOString(), has_more: false, completions: transcript }),
    }))
    await page.goto(`${base}/c/rtl-fixture`, { waitUntil: 'domcontentloaded', timeout: 120000 })
    await page.locator('ul > li table').nth(3).waitFor({ timeout: 120000 })
    await page.addStyleTag({ content: '[data-testid="jump-to-latest"]{display:none!important}' })
    const sharedItems = page.locator('ul > li:has(.markdown-wrapper table)')
    for (const [i, id] of ['a-pending', 'a-wrong', 'a-he', 'a-en'].entries()) {
      await sharedItems.nth(i).scrollIntoViewIfNeeded()
      await sharedItems.nth(i).screenshot({ path: `${out}/${phase}-${locale}-shared-${id}.png` })
    }
    if (phase === 'after') {
      const shared = await page.evaluate(() => [...document.querySelectorAll('ul > li')]
        .filter((li) => li.querySelector('.markdown-wrapper table'))
        .map((li) => {
          const sel = '.markdown-wrapper, .table-node-wrapper, table, th, td, ul, li, .markdown-wrapper p, .tool-execution-container'
          return [getComputedStyle(li).direction, ...[...li.querySelectorAll(sel)].map((el) => getComputedStyle(el).direction)]
        }))
      const want = ['rtl', 'rtl', 'rtl', 'ltr']
      if (shared.length !== 4) failures.push(`${locale}/shared: ${shared.length} replies`)
      shared.forEach((dirs, i) => { if (dirs.some((d) => d !== want[i])) failures.push(`${locale}/shared reply ${i}: ${JSON.stringify(dirs)}`) })
    }
    if (errors.length) failures.push(`${locale}: page errors ${errors.join(' | ')}`)
    await context.close()
  }
} finally {
  await browser.close()
}
if (failures.length) {
  console.error(failures.join('\n'))
  process.exit(1)
}
console.log(`rtl-message-direction (${phase}): ok → ${out}`)
