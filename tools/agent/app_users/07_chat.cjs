// An app user chats inside an app (the platform chat bubble), real LLM.
const { open, check, done, BOW } = require('./lib.cjs')
const out = process.argv[2]
;(async () => {
  const { browser, context } = await open({ storageState: out + '/../member-state.json' })
  const page = await context.newPage(); page.setDefaultTimeout(180000)
  const api = []
  page.on('response', r => { if (r.url().includes('/api/')) api.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`) })
  await page.goto(BOW + '/apps')
  await page.locator('[data-testid="app-card"]', { hasText: 'Customer Health' }).click()
  await page.getByTestId('artifact-chat-bubble').waitFor()
  await page.getByTestId('artifact-chat-bubble').click()
  await page.getByTestId('chat-input').fill('How many customers are in the music store? Answer with the number.')
  await page.getByTestId('chat-send').click()
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-testid="chat-messages"]'); const input = document.querySelector('[data-testid="chat-input"]')
    return el && /\b59\b/.test(el.innerText) && input && !/Answering/.test(input.getAttribute('placeholder') || '')
  }, null, { timeout: 240000 }).catch(() => {})
  const text = await page.getByTestId('chat-messages').innerText()
  check('the agent answers the app user from the data (59 customers)', /\b59\b/.test(text), text.replace(/\s+/g, ' ').slice(-300))
  await page.waitForTimeout(1500)
  await page.screenshot({ path: `${out}/60-app-user-chat.png` })
  const refused = api.filter(l => /^(403|401|500)/.test(l))
  check('nothing the chat needed was refused', refused.length === 0, refused.join(' | '))
  await browser.close(); done(`${out}/07-results.json`); process.exit(process.exitCode || 0)
})().catch(e => { console.error(e); process.exit(2) })
