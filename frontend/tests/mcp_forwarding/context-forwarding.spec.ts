import { test, expect, Page } from '@playwright/test'
import * as fs from 'fs'
import * as path from 'path'

// End-to-end UI test for MCP user-context forwarding, driven entirely through
// the real UI from 0→1 (fresh signup). It creates an MCP connection pointed at
// the local echo MCP server (tests/mocks/echo_mcp_http_server.py on :3333),
// configures header + metadata forwarding in the Advanced section, verifies the
// live Test Connection, saves, and asserts the persisted config via the API.
//
// Screenshots land in test-results/mcp-forwarding/ for the PR evidence.

const SHOTS = path.resolve('test-results/mcp-forwarding')
const ECHO_URL = process.env.ECHO_MCP_URL || 'http://127.0.0.1:3333/mcp'

// Unique email per run: this suite runs against a freshly-reset DB (see
// tools/agent/reset_backend.sh), so signup always claims the bootstrap admin.
const ADMIN = {
  name: 'MCP Fwd Admin',
  email: `mcp-fwd-${Date.now()}@example.com`,
  password: 'TestPass123!',
}

async function shot(page: Page, name: string) {
  fs.mkdirSync(SHOTS, { recursive: true })
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true })
}

async function signUp(page: Page) {
  await page.goto('/users/sign-up', { waitUntil: 'load' })
  await page.waitForSelector('#name', { state: 'visible', timeout: 30000 })
  await page.waitForLoadState('networkidle').catch(() => {})
  await page.fill('#name', ADMIN.name)
  await page.fill('#email', ADMIN.email)
  await page.fill('#password', ADMIN.password)
  await page.click('button[type="submit"]')
  const result = await Promise.race([
    page.waitForURL((u) => !u.pathname.includes('/users/sign-up'), { timeout: 45000 }).then(() => 'ok'),
    page.waitForSelector('.text-red-500', { timeout: 45000 }).then(() => 'error').catch(() => 'ok'),
  ])
  if (result === 'error') {
    const msg = await page.locator('.text-red-500').first().textContent().catch(() => '')
    throw new Error(`Sign-up failed: ${msg}`)
  }
  await page.waitForLoadState('domcontentloaded')
}

async function dismissOnboarding(page: Page) {
  for (let i = 0; i < 6; i++) {
    if (!page.url().includes('/onboarding')) return
    await page.waitForLoadState('networkidle').catch(() => {})
    const skip = page.getByRole('button', { name: 'Skip onboarding' })
    if (await skip.isVisible({ timeout: 8000 }).catch(() => false)) {
      await skip.click()
      const left = await page
        .waitForURL((u) => !u.pathname.includes('/onboarding'), { timeout: 8000 })
        .then(() => true).catch(() => false)
      if (left) return
    }
  }
}

// This spec is standalone: it needs the local echo MCP server (pw.mcp.config.ts)
// and a fresh unauthenticated signup. If the echo server isn't reachable (e.g.
// the shared CI Playwright run), skip rather than fail — the same env-gating the
// live backend test uses.
test.beforeEach(async () => {
  const up = await fetch(ECHO_URL).then(() => true).catch(() => false)
  test.skip(!up, `echo MCP server not reachable at ${ECHO_URL} — run via pw.mcp.config.ts`)
})

test('MCP context forwarding: configure, test, save through the UI', async ({ page }) => {
  // Capture the app's own auth headers (nuxt-auth token + org id) from any API
  // call it makes, so we can re-query /api/connections authenticated afterwards.
  let apiHeaders: Record<string, string> = {}
  page.on('request', (req) => {
    const h = req.headers()
    if (req.url().includes('/api/') && h['authorization']) {
      apiHeaders = { Authorization: h['authorization'] }
      if (h['x-organization-id']) apiHeaders['X-Organization-Id'] = h['x-organization-id']
    }
  })

  // ── 0→1: brand-new admin (fresh DB) ────────────────────────────────
  await signUp(page)
  await dismissOnboarding(page)

  // ── open the MCP connection form ────────────────────────────────────
  await page.goto('/agents/new', { waitUntil: 'load' })
  await page.waitForLoadState('networkidle').catch(() => {})
  await shot(page, '00-agents-new')

  // The Add Connection modal auto-opens when the org has no connections;
  // otherwise there's a "Create new connection" button to open it.
  const mcpTile = page.getByRole('button', { name: 'MCP Server', exact: true })
  if (!(await mcpTile.isVisible({ timeout: 8000 }).catch(() => false))) {
    const createConn = page.getByRole('button', { name: 'Create new connection' })
    if (await createConn.isVisible({ timeout: 8000 }).catch(() => false)) {
      await createConn.click()
    }
  }
  await expect(mcpTile).toBeVisible({ timeout: 20000 })
  await mcpTile.click()

  // Modal step 2: the MCP form.
  await expect(page.locator('[data-test="mcp-name"]')).toBeVisible({ timeout: 15000 })
  await page.locator('[data-test="mcp-name"]').fill('Echo LN MCP')
  await page.locator('[data-test="mcp-url"]').fill(ECHO_URL)
  await page.locator('[data-test="mcp-transport"]').selectOption('streamable_http')
  await shot(page, '01-form-filled')

  // ── Advanced → user context forwarding ──────────────────────────────
  await page.locator('[data-test="mcp-advanced-toggle"]').click()
  await expect(page.locator('[data-test="mcp-forwarding"]')).toBeVisible({ timeout: 10000 })

  // Header rule: X-User-Email ← user.email
  await page.locator('[data-test="add-header"]').click()
  const hRow = page.locator('[data-test="header-row-0"]')
  await hRow.locator('[data-test="header-name"]').fill('X-User-Email')
  await hRow.locator('[data-test="header-source"]').selectOption('user.email')

  // Metadata field 1 (locked): _client_userId ← membership.attr:employeeId
  await page.locator('[data-test="add-meta"]').click()
  const m0 = page.locator('[data-test="meta-row-0"]')
  await m0.locator('[data-test="meta-name"]').fill('_client_userId')
  await m0.locator('[data-test="meta-source"]').selectOption('membership.attr')
  await m0.locator('[data-test="meta-key"]').fill('employeeId')
  // stays locked (default) — assert the badge shows the lock
  await expect(m0.locator('[data-test="meta-mode"]')).toContainText('Locked')

  // Metadata field 2 (AI-fillable): department ← membership.role
  await page.locator('[data-test="add-meta"]').click()
  const m1 = page.locator('[data-test="meta-row-1"]')
  await m1.locator('[data-test="meta-name"]').fill('department')
  await m1.locator('[data-test="meta-source"]').selectOption('membership.role')
  await m1.locator('[data-test="meta-mode"]').click() // toggle to AI
  await expect(m1.locator('[data-test="meta-mode"]')).toContainText('AI')

  await shot(page, '02-forwarding-configured')

  // ── live Test Connection against the echo server ────────────────────
  const testBtn = page.getByRole('button', { name: /Test Connection|Verify/ })
  await testBtn.click()
  // Success banner (green) — the backend reached the echo server and listed tools.
  await expect(page.getByText(/tool\(s\)|connected|success/i).first())
    .toBeVisible({ timeout: 30000 })
  await shot(page, '03-test-connection-success')

  // ── save the connection ─────────────────────────────────────────────
  const connect = page.getByRole('button', { name: /^(Connect|Add connection|Save)$/ })
  await connect.click()

  // The modal advances past the form (indexing/success) — the form inputs go away.
  await expect(page.locator('[data-test="mcp-name"]')).toBeHidden({ timeout: 30000 })
  await shot(page, '04-connection-created')

  // ── assert the persisted config via the API ─────────────────────────
  expect(apiHeaders.Authorization, 'captured app auth headers').toBeTruthy()
  const list = await page.request.get('/api/connections', { headers: apiHeaders })
  expect(list.ok()).toBeTruthy()
  const conns = await list.json()
  const conn = (Array.isArray(conns) ? conns : conns.items || []).find((c: any) => c.name === 'Echo LN MCP')
  expect(conn, 'created MCP connection should exist').toBeTruthy()

  const detailRes = await page.request.get(`/api/connections/${conn.id}`, { headers: apiHeaders })
  expect(detailRes.ok()).toBeTruthy()
  const detail = await detailRes.json()
  const cfg = detail.config || {}

  // Header forwarding persisted
  expect(cfg.header_injection).toEqual([
    { header: 'X-User-Email', source: 'user.email' },
  ])
  // Metadata forwarding persisted with correct modes + source grammar
  expect(cfg.metadata_injection?.argument_key).toBe('custom_metadata')
  const fields = cfg.metadata_injection?.fields || []
  const locked = fields.find((f: any) => f.name === '_client_userId')
  const ai = fields.find((f: any) => f.name === 'department')
  expect(locked).toMatchObject({ source: 'membership.attr:employeeId', mode: 'locked' })
  expect(ai).toMatchObject({ source: 'membership.role', mode: 'ai' })
})
