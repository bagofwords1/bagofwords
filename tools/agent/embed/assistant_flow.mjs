// The chat-assistant artifact inside the back office, used like an employee:
// ask, get an answer from the org's agent (real LLM), start a second
// conversation, come back to the first; another employee sees none of it.
//   node tools/agent/embed/assistant_flow.mjs <out_dir>
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
const require = createRequire(new URL('../../../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')

const out = process.argv[2]
const ERP = process.env.ERP_URL || 'http://localhost:4173'
mkdirSync(out, { recursive: true })
const results = []
const check = (name, ok, detail = '') => { results.push({ name, ok, detail: String(detail).slice(0, 400) }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${String(detail).slice(0, 300)}`) }
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })

async function open(user) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  page.setDefaultTimeout(240000)
  await page.goto(`${ERP}/signin?user=${user}&next=/assistant`)
  await page.getByTestId('bow-status').filter({ hasText: 'signed in as' }).waitFor()
  const bow = await (await page.locator('iframe').first().elementHandle()).contentFrame()
  await bow.locator('iframe').first().waitFor()
  const app = await (await bow.locator('iframe').first().elementHandle()).contentFrame()
  await app.locator('textarea').waitFor()
  await page.waitForTimeout(2000)
  return { context, page, bow, app }
}

// The chat pane only: the textarea's widest ancestor that is not the sidebar.
const pane = (app) => app.evaluate(() => {
  let el = document.querySelector('textarea'), best = el
  while (el && el.parentElement && el.parentElement.getBoundingClientRect().left > 200) { el = el.parentElement; best = el }
  return best.innerText
})

async function ask(app, question) {
  const before = await app.locator('body').innerText()
  await app.locator('textarea').fill(question)
  await app.getByRole('button', { name: /send/i }).click()
  // Answered when the conversation stops changing for a while after the reply grew.
  let last = '', stableSince = Date.now(), started = Date.now()
  while (Date.now() - started < 300000) {
    await app.waitForTimeout(1000)
    const text = await pane(app)
    const tail = text.includes(question) ? text.slice(text.lastIndexOf(question)) : ''
    if (text !== last) { last = text; stableSince = Date.now(); continue }
    if (tail.length > question.length + 80 && Date.now() - stableSince > 8000) break
  }
  const after = await pane(app)
  return after.includes(question) ? after.slice(after.indexOf(question)) : ''
}

const bubbles = async (bow) => bow.locator('[data-testid="artifact-chat-bubble"], button[aria-label*="hat"]').count()

// Alice
const alice = await open('alice')
check('no second chat bubble over the app', (await alice.bow.locator('.fixed.bottom-5 button, [data-testid^="artifact-chat"]').count()) === 0)
const a1 = await ask(alice.app, 'Who were our top 5 customers by total spend last year, and which countries are they in?')
check('alice gets an answer from the data (Q1)', a1.length > 120 && /\$|\d{2,}/.test(a1), a1.replace(/\s+/g, ' ').slice(0, 300))
await alice.page.screenshot({ path: `${out}/21-assistant-answer.png` })
await alice.app.getByRole('button', { name: /new conversation/i }).click()
await alice.page.waitForTimeout(1500)
const a2 = await ask(alice.app, 'Which genres sell best in Brazil?')
check('alice gets an answer in a second conversation (Q2)', a2.length > 80, a2.replace(/\s+/g, ' ').slice(0, 300))
const listText = await alice.app.locator('body').innerText()
await alice.page.screenshot({ path: `${out}/22-assistant-two-conversations.png` })
// Back to the first one from the list.
const first = alice.app.getByText(/top 5 customers|Top 5|customers by total spend/i).first()
await first.click().catch(() => {})
await alice.page.waitForTimeout(3000)
const reopened = await alice.app.locator('body').innerText()
check('the first conversation reopens with its history', /top 5 customers/i.test(reopened), '')
await alice.page.screenshot({ path: `${out}/23-assistant-reopened.png` })
await alice.context.close()

// Bob sees none of Alice's conversations.
const bob = await open('bob')
await bob.page.waitForTimeout(3000)
const bobText = await bob.app.locator('body').innerText()
check("bob does not see alice's conversations", !/top 5 customers|Brazil/i.test(bobText), bobText.replace(/\s+/g, ' ').slice(0, 200))
await bob.page.screenshot({ path: `${out}/24-assistant-bob-empty.png` })
await bob.context.close()

writeFileSync(`${out}/assistant_flow.json`, JSON.stringify(results, null, 1))
await browser.close()
process.exit(results.every(r => r.ok) ? 0 : 1)
