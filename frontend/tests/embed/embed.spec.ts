import { test, expect, type Page, type Frame, type BrowserContext } from '@playwright/test'
import * as fs from 'fs'
import * as path from 'path'

// Embedded chat, driven like an employee inside a customer application.
//
// The "customer application" is tools/agent/embed/erp_host.py (Northwind ERP)
// on its own origin (:4173). It loads the SDK from BOW (:3000), mounts the
// assistant, and signs assertions on its backend for whoever is signed in to
// the ERP. BOW maps the assertion subject to a member through the admin-set
// `employee_id` attribute, runs the stub LLM (:9099) which calls the echo MCP
// server (:3333) through the "ERP orders" agent, and the answer quotes the
// identity the MCP server actually received.
//
// Evidence lands in media/pr/embedded-chat/ (screenshots + a video of the
// main journey). Sandbox setup: docs/feedback-loops/embedded-chat.md.

const BOW = process.env.BOW_BASE_URL || 'http://localhost:3000'
const ERP = process.env.ERP_BASE_URL || 'http://127.0.0.1:4173'
const SEED = JSON.parse(fs.readFileSync(process.env.EMBED_SEED || '/tmp/bow-agent/embed.json', 'utf8'))
const CAPTURE = process.env.MOCK_MCP_CAPTURE_FILE || '/tmp/bow-agent/mcp_capture.json'
const SHOTS = path.resolve(process.cwd(), '../media/pr/embedded-chat')
const EMBED_ID: string = SEED.embed.public_id
const ALLOWED_HOSTS = new Set([new URL(BOW).host, new URL(ERP).host, 'localhost:4173'])

fs.mkdirSync(SHOTS, { recursive: true })

async function shot(page: Page, name: string) {
  await page.waitForTimeout(400)
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) })
}

function chatFrame(page: Page): Frame {
  const frame = page.frames().find(f => f.url().includes(`/embed/${EMBED_ID}`))
  if (!frame) throw new Error('embed frame not found')
  return frame
}

async function waitForFrame(page: Page): Promise<Frame> {
  await expect.poll(() => page.frames().some(f => f.url().includes(`/embed/${EMBED_ID}`)), { timeout: 30000 }).toBe(true)
  const frame = chatFrame(page)
  await frame.locator('[data-testid=embed-root]').waitFor({ timeout: 60000 })
  return frame
}

async function openPanel(page: Page): Promise<Frame> {
  const frame = await waitForFrame(page)
  if (await frame.locator('[data-testid=embed-launcher]').isVisible().catch(() => false)) {
    await frame.locator('[data-testid=embed-launcher]').click()
  }
  await frame.locator('[data-testid=embed-panel]').waitFor({ timeout: 20000 })
  return frame
}

async function waitSignedIn(frame: Frame, name: string) {
  await expect(frame.locator('[data-testid=embed-user]')).toHaveText(name, { timeout: 40000 })
  await expect(frame.locator('[data-testid=chat-input]')).toBeVisible({ timeout: 20000 })
  // The persisted thread is fetched right after sign-in; counts are meaningless before that.
  await expect(frame.locator('[data-testid=embed-panel]')).toHaveAttribute('data-history-loaded', 'true', { timeout: 30000 })
}

async function send(frame: Frame, text: string) {
  await frame.locator('[data-testid=chat-input]').fill(text)
  await frame.locator('[data-testid=chat-send]').click()
}

// Click the matching suggested prompt on a fresh thread, else type it: the
// loop stays runnable on a thread that already has history.
async function ask(frame: Frame, text: string) {
  const suggestion = frame.locator('[data-testid=embed-suggestions] button', { hasText: text })
  if (await suggestion.isVisible().catch(() => false)) await suggestion.click()
  else await send(frame, text)
}

const userMessages = (frame: Frame) => frame.locator('[data-testid=chat-user-message]')

async function lastAnswer(frame: Frame): Promise<ReturnType<Frame['locator']>> {
  return frame.locator('[data-testid=chat-assistant-message]').last()
}

async function waitAnswerContaining(frame: Frame, text: string | RegExp, timeout = 90000) {
  const answer = await lastAnswer(frame)
  await expect(answer).toContainText(text, { timeout })
  // The composer is enabled again once the stream ended.
  await expect(frame.locator('[data-testid=chat-input]')).toBeEnabled({ timeout: 30000 })
  return answer
}

function readCapture(): any {
  return JSON.parse(fs.readFileSync(CAPTURE, 'utf8'))
}

function recordNetwork(context: BrowserContext): () => string[] {
  const hosts = new Set<string>()
  context.on('request', req => {
    try { hosts.add(new URL(req.url()).host) } catch { /* data: etc. */ }
  })
  return () => [...hosts].sort()
}



test('floating assistant: alice → context → reload → bob → unmapped carol', async ({ browser }) => {
  const context = await browser.newContext({
    viewport: { width: 1360, height: 860 },
    recordVideo: { dir: path.join(SHOTS, '.video'), size: { width: 1360, height: 860 } },
  })
  const hostsSeen = recordNetwork(context)
  const page = await context.newPage()
  if (fs.existsSync(CAPTURE)) fs.unlinkSync(CAPTURE)

  // ── alice ───────────────────────────────────────────────────────────
  await page.goto(`${ERP}/?user=alice`, { waitUntil: 'load' })
  const frame0 = await waitForFrame(page)
  await expect(frame0.locator('[data-testid=embed-launcher]')).toBeVisible({ timeout: 30000 })
  await shot(page, '01-erp-launcher')

  let frame = await openPanel(page)
  await waitSignedIn(frame, 'Alice Nguyen')
  const aliceBefore = await userMessages(frame).count()
  if (aliceBefore === 0) await expect(frame.locator('[data-testid=embed-greeting]')).toContainText('production orders')
  await expect(frame.locator('[data-testid=embed-can-query]')).toContainText('ERP orders')
  await shot(page, '02-greeting-signed-in')
  // The SDK heard about the sign-in without seeing any transcript.
  const events = await page.evaluate(() => (window as any).__bow_events.map((e: any) => e.ev))
  expect(events).toEqual(expect.arrayContaining(['ready', 'open', 'authenticated']))

  await ask(frame, 'What are my open production orders?')
  await shot(page, '03-streaming')
  let answer = await waitAnswerContaining(frame, 'E-1001')
  await expect(answer).toContainText('incident-review')
  await shot(page, '04-answer-alice')
  let cap = readCapture()
  expect(cap.received_arguments.custom_metadata).toMatchObject({
    employee_id: 'E-1001', user_email: 'alice@erp.example', workflow_id: 'incident-review', erp_user: 'alice',
  })
  expect(cap.received_headers['x-employee-id']).toBe('E-1001')

  // Host context: pick a record, ask about it; the record id reaches the turn.
  await page.locator('[data-testid=erp-row-INC-1042]').click()
  await expect(page.locator('#selected')).toHaveText('INC-1042')
  await send(frame, 'Summarize this incident')
  answer = await waitAnswerContaining(frame, 'INC-1042')
  await shot(page, '05-answer-with-record-context')

  // Reload: the thread is persisted per (embed, user) and comes back.
  await page.reload({ waitUntil: 'load' })
  frame = await openPanel(page)
  await waitSignedIn(frame, 'Alice Nguyen')
  await expect(userMessages(frame)).toHaveCount(aliceBefore + 2, { timeout: 30000 })
  await expect(frame.locator('[data-testid=chat-assistant-message]').last()).toContainText('INC-1042')
  await shot(page, '06-history-after-reload')

  // ── bob: user switch ends alice's session; bob sees only his own thread ──
  await page.locator('[data-testid=erp-user-switch]').selectOption('bob')
  await page.waitForURL(/user=bob/, { timeout: 20000 })
  await page.waitForLoadState('load')
  frame = await openPanel(page)
  await waitSignedIn(frame, 'Bob Okafor')
  // Bob's own thread: nothing scoped to alice's identity, ever (and empty
  // right after a reseed).
  const bobBefore = await userMessages(frame).count()
  await expect(frame.locator('[data-testid=chat-messages]')).not.toContainText('E-1001')
  await expect(frame.locator('[data-testid=chat-messages]')).not.toContainText('alice')
  if (bobBefore === 0) await expect(frame.locator('[data-testid=embed-greeting]')).toBeVisible()
  await ask(frame, 'What are my open production orders?')
  answer = await waitAnswerContaining(frame, 'E-1002')
  await expect(answer).not.toContainText('E-1001')
  await expect(userMessages(frame)).toHaveCount(bobBefore + 1)
  cap = readCapture()
  expect(cap.received_arguments.custom_metadata.employee_id).toBe('E-1002')
  expect(cap.received_arguments.custom_metadata.erp_user).toBe('bob')
  await shot(page, '07-answer-bob')

  // ── carol: a real ERP user with no BOW mapping → clear, non-leaky refusal ──
  await page.locator('[data-testid=erp-user-switch]').selectOption('carol')
  await page.waitForURL(/user=carol/, { timeout: 20000 })
  await page.waitForLoadState('load')
  frame = await openPanel(page)
  await expect(frame.locator('[data-testid=embed-auth-required]')).toBeVisible({ timeout: 40000 })
  await expect(frame.locator('[data-testid=embed-auth-error]')).toContainText('not mapped')
  await expect(frame.locator('[data-testid=chat-input]')).toHaveCount(0)
  await shot(page, '08-unmapped-identity')

  // ── egress: everything came from the ERP origin or the BOW origin ──
  const hosts = hostsSeen()
  expect(hosts.filter(h => !ALLOWED_HOSTS.has(h))).toEqual([])
  fs.writeFileSync(path.join(SHOTS, 'network-hosts.json'), JSON.stringify(hosts, null, 2))

  await context.close()
  const videos = fs.readdirSync(path.join(SHOTS, '.video')).filter(f => f.endsWith('.webm'))
  if (videos.length) fs.renameSync(path.join(SHOTS, '.video', videos[videos.length - 1]), path.join(SHOTS, 'journey.webm'))
})

test('inline mode, dark theme and Hebrew (RTL) through host options', async ({ page }) => {
  await page.goto(`${ERP}/inline?user=alice&theme=dark`, { waitUntil: 'load' })
  let frame = await waitForFrame(page)
  await expect(frame.locator('[data-testid=embed-panel]')).toBeVisible({ timeout: 30000 })
  await expect(frame.locator('[data-testid=embed-launcher]')).toHaveCount(0)
  await waitSignedIn(frame, 'Alice Nguyen')
  expect(await frame.evaluate(() => document.documentElement.classList.contains('dark'))).toBe(true)
  // Alice's thread again (same embed, same user): the persisted turns render,
  // markdown included — wait for parsed text, not just the bubbles.
  await expect(frame.locator('[data-testid=chat-user-message]').first()).toBeVisible({ timeout: 30000 })
  await expect(frame.locator('[data-testid=chat-assistant-message]').last()).toContainText('E-1001', { timeout: 30000 })
  await shot(page, '09-inline-dark')

  await page.goto(`${ERP}/inline?user=alice&locale=he`, { waitUntil: 'load' })
  frame = await waitForFrame(page)
  await waitSignedIn(frame, 'Alice Nguyen')
  await expect.poll(() => frame.evaluate(() => document.documentElement.getAttribute('dir'))).toBe('rtl')
  await expect(frame.locator('[data-testid=chat-input]')).toHaveAttribute('placeholder', /שאל/)
  await expect(frame.locator('[data-testid=chat-assistant-message]').last()).toContainText('E-1001', { timeout: 30000 })
  await shot(page, '10-inline-hebrew-rtl')
})

test('a tampered assertion is refused and the user sees why', async ({ page }) => {
  await page.route('**/internal/bow/assertion', async route => {
    const res = await route.fetch()
    const body = await res.json()
    const parts = body.assertion.split('.')
    // Flip the payload: subject becomes someone else, signature no longer matches.
    const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString())
    payload.sub = 'E-1002'
    parts[1] = Buffer.from(JSON.stringify(payload)).toString('base64url')
    await route.fulfill({ json: { assertion: parts.join('.') } })
  })
  await page.goto(`${ERP}/?user=alice`, { waitUntil: 'load' })
  const frame = await openPanel(page)
  await expect(frame.locator('[data-testid=embed-auth-required]')).toBeVisible({ timeout: 40000 })
  await expect(frame.locator('[data-testid=embed-auth-error]')).toContainText('not accepted')
  await shot(page, '11-tampered-assertion')
})

test('a page on an unlisted origin cannot frame the chat', async ({ page }) => {
  // Observable invariants: the chat document answers with a frame-ancestors
  // policy that excludes this origin, the browser never runs it (no bootstrap
  // call, no rendered root), and the ERP origin IS in the policy.
  const frameRequests: string[] = []
  page.on('request', req => { if (req.url().includes(`/api/embed/${EMBED_ID}/`)) frameRequests.push(req.url()) })
  // Chromium drops the response event for a frame it refuses to render, so
  // read the document's policy with a plain request.
  const doc = await page.request.get(`${BOW}/embed/${EMBED_ID}?mode=inline`, { headers: { accept: 'text/html' } })
  const csp = doc.headers()['content-security-policy'] || ''
  await page.route('http://evil.example/**', route => route.fulfill({
    contentType: 'text/html',
    body: `<html><body><h1>Not the ERP</h1><iframe id="f" src="${BOW}/embed/${EMBED_ID}?mode=inline" width="400" height="500"></iframe></body></html>`,
  }))
  await page.goto('http://evil.example/', { waitUntil: 'load' })
  await page.waitForTimeout(4000)
  const framed = page.frames().filter(f => f.url().includes(`/embed/${EMBED_ID}`))
  const rendered = framed.length ? await framed[0].locator('[data-testid=embed-root]').count().catch(() => 0) : 0
  expect(rendered).toBe(0)
  expect(frameRequests).toEqual([])
  expect(csp).toContain('frame-ancestors')
  expect(csp).toContain(new URL(ERP).origin)
  expect(csp).not.toContain('evil.example')
  await shot(page, '12-unlisted-origin-blocked')
})

test('settings: configure, preview as the admin, install snippet', async ({ page }) => {
  await page.goto(`${BOW}/users/sign-in`, { waitUntil: 'load' })
  await page.fill('#email', SEED.admin.email)
  await page.fill('#password', SEED.admin.password)
  await page.click('button[type="submit"]')
  await page.waitForURL(u => !u.pathname.includes('/users/sign-in'), { timeout: 45000 })
  await page.goto(`${BOW}/settings/embeds`, { waitUntil: 'load' })
  await expect(page.locator('[data-testid=embed-heading]')).toHaveText('Northwind ERP', { timeout: 40000 })
  await expect(page.locator('[data-testid=embed-public-id]')).toHaveText(EMBED_ID)
  await shot(page, '13-settings-overview')
  await page.click('[data-testid=embed-tab-runtime]')
  await expect(page.locator('[data-testid=embed-agents] input:checked')).toHaveCount(1)
  await shot(page, '14-settings-runtime')
  await page.click('[data-testid=embed-tab-auth]')
  await expect(page.locator('[data-testid=embed-members]')).toContainText('E-1001')
  await expect(page.locator('[data-testid=embed-members]')).toContainText('E-1002')
  await shot(page, '15-settings-auth')
  await page.click('[data-testid=embed-tab-appearance]')
  await shot(page, '16-settings-appearance')
  await page.click('[data-testid=embed-tab-install]')
  await expect(page.locator('[data-testid=embed-snippet]')).toContainText(EMBED_ID)
  await expect(page.locator('[data-testid=embed-snippet]')).toContainText('/api/embed/sdk/v1/bow-embed.js')
  await shot(page, '17-settings-install')

  // Preview: the real renderer, signed in as the admin (never as someone else).
  await page.click('[data-testid=embed-tab-appearance]')
  await page.click('[data-testid=embed-preview]')
  const preview = page.frameLocator('[data-testid=embed-preview-frame]')
  await expect(preview.locator('[data-testid=embed-user]')).toHaveText(/Agent Admin/, { timeout: 40000 })
  await expect(preview.locator('[data-testid=embed-greeting]')).toBeVisible()
  await page.locator('[data-testid=embed-preview-frame]').scrollIntoViewIfNeeded()
  await shot(page, '18-settings-preview')
})

test('artifact-page chat still works on the shared compact components', async ({ page }) => {
  // The /r/{id} bubble was refactored onto components/chat/*; prove the
  // existing viewer flow (open, send, streamed answer) is unchanged.
  const login = await page.request.post(`${BOW}/api/auth/jwt/login`, { form: { username: SEED.admin.email, password: SEED.admin.password } })
  const token = (await login.json()).access_token
  const headers = { Authorization: `Bearer ${token}`, 'X-Organization-Id': SEED.org_id }
  const report = await (await page.request.post(`${BOW}/api/reports`, { headers, data: { title: 'Embed regression', files: [], data_sources: [] } })).json()
  const vis = await page.request.put(`${BOW}/api/reports/${report.id}/visibility/artifact`, { headers, data: { visibility: 'internal', artifact_chat_enabled: true } })
  expect(vis.ok()).toBeTruthy()

  await page.goto(`${BOW}/users/sign-in`, { waitUntil: 'load' })
  await page.fill('#email', SEED.admin.email)
  await page.fill('#password', SEED.admin.password)
  await page.click('button[type="submit"]')
  await page.waitForURL(u => !u.pathname.includes('/users/sign-in'), { timeout: 45000 })
  await page.goto(`${BOW}/r/${report.id}`, { waitUntil: 'load' })
  await page.locator('[data-testid=artifact-chat-bubble]').click({ timeout: 40000 })
  await expect(page.locator('[data-testid=artifact-chat-panel]')).toBeVisible()
  await page.locator('[data-testid=chat-input]').fill('hello from the shared components')
  await page.locator('[data-testid=chat-send]').click()
  await expect(page.locator('[data-testid=chat-user-message]')).toHaveCount(1)
  await expect(page.locator('[data-testid=chat-assistant-message]').last()).toContainText(/orders|tool/i, { timeout: 90000 })
  await expect(page.locator('[data-testid=chat-input]')).toBeEnabled({ timeout: 30000 })
  await shot(page, '19-artifact-chat-regression')
})
