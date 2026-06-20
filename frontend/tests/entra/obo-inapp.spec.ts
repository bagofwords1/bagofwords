import { test, expect, request as pwRequest, APIRequestContext } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

// In-app OBO flows that do NOT require live Fabric SQL (port 1433 is blocked in
// the sandbox): connection creation with a service account + user_required/oauth,
// and the admin query-identity toggle (service_account <-> self). The actual
// per-user table ACLs need 1433 egress and are covered by the backend tier.

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const API = process.env.BOW_API_BASE || 'http://localhost:8000';
const FABRIC_SERVER = process.env.BOW_FABRIC_SERVER || 'test.datawarehouse.fabric.microsoft.com';
const FABRIC_DB = process.env.BOW_FABRIC_DATABASE || 'demo_db';
const TENANT = process.env.BOW_ENTRA_TENANT_ID || '';
const CLIENT = process.env.BOW_ENTRA_CLIENT_ID || '';
const SECRET = process.env.BOW_ENTRA_CLIENT_SECRET || '';

let api: APIRequestContext;
let orgId = '';
let token = '';

function readToken(): string {
  const p = path.join(__dirname, '.auth', 'token.json');
  if (!fs.existsSync(p)) return '';
  return JSON.parse(fs.readFileSync(p, 'utf-8')).token || '';
}

test.beforeAll(async () => {
  token = readToken();
  test.skip(!token, 'no captured session token (auth.setup did not run)');
  api = await pwRequest.newContext({
    baseURL: API,
    ignoreHTTPSErrors: true,
    extraHTTPHeaders: { Authorization: `Bearer ${token}` },
  });
  const orgsRes = await api.get('/api/organizations');
  expect(orgsRes.ok(), `GET /api/organizations -> ${orgsRes.status()}`).toBeTruthy();
  const orgs = await orgsRes.json();
  orgId = orgs[0]?.organization?.id || orgs[0]?.id;
  expect(orgId, 'expected an organization id').toBeTruthy();
  // Re-create context with org header for all subsequent calls.
  await api.dispose();
  api = await pwRequest.newContext({
    baseURL: API,
    ignoreHTTPSErrors: true,
    extraHTTPHeaders: { Authorization: `Bearer ${token}`, 'X-Organization-Id': orgId },
  });
  // Dismiss onboarding so /instructions and app routes load instead of redirecting.
  await api.put('/api/organization/onboarding', { data: { dismissed: true, completed: true } });
});

test.afterAll(async () => { await api?.dispose(); });

async function createConnection(name: string, authPolicy: string, modes: string[]) {
  const res = await api.post('/api/connections', {
    data: {
      name,
      type: 'ms_fabric',
      config: { server_hostname: FABRIC_SERVER, database: FABRIC_DB },
      credentials: { tenant_id: TENANT, client_id: CLIENT, client_secret: SECRET },
      auth_policy: authPolicy,
      allowed_user_auth_modes: modes,
    },
  });
  return res;
}

test('C1/C4: admin creates a Fabric service-account connection (user_required + oauth)', async () => {
  const res = await createConnection(`Fabric OBO ${Date.now()}`, 'user_required', ['oauth']);
  expect(res.ok(), `POST /api/connections -> ${res.status()}: ${await res.text()}`).toBeTruthy();
  const conn = await res.json();
  expect(conn.type).toBe('ms_fabric');
  expect(conn.auth_policy).toBe('user_required');
  // C4: secret must not be echoed back in plaintext.
  expect(JSON.stringify(conn)).not.toContain(SECRET);
  // The create response omits allowed_user_auth_modes (F-7); confirm it persisted
  // by fetching the connection list.
  const list = await api.get('/api/connections');
  expect(list.ok()).toBeTruthy();
  const found = (await list.json()).find((c: any) => c.id === conn.id);
  expect(found, 'created connection should be listed').toBeTruthy();
  expect(found.allowed_user_auth_modes).toContain('oauth');
});

test('G1/G2: admin switches query identity service_account <-> self', async () => {
  const created = await createConnection(`Fabric Toggle ${Date.now()}`, 'user_required', ['oauth']);
  expect(created.ok()).toBeTruthy();
  const id = (await created.json()).id;

  // -> service_account => effective_auth resolves to the system service principal.
  const toSvc = await api.patch(`/api/connections/${id}/query-identity`, { data: { query_identity: 'service_account' } });
  expect(toSvc.ok(), `PATCH service_account -> ${toSvc.status()}: ${await toSvc.text()}`).toBeTruthy();
  const svc = await toSvc.json();
  console.log('[inapp] service_account status:', JSON.stringify(svc));
  expect(svc.query_identity).toBe('service_account');
  expect(svc.effective_auth).toBe('system');
  expect(svc.can_switch_identity).toBeTruthy();

  // -> self => not the service account anymore (user token or none, since this
  //    connection was created after login so no OBO creds were auto-provisioned).
  const toSelf = await api.patch(`/api/connections/${id}/query-identity`, { data: { query_identity: 'self' } });
  expect(toSelf.ok(), `PATCH self -> ${toSelf.status()}`).toBeTruthy();
  const self = await toSelf.json();
  console.log('[inapp] self status:', JSON.stringify(self));
  expect(self.query_identity).toBe('self');
  expect(['user', 'none']).toContain(self.effective_auth);
  expect(self.effective_auth).not.toBe('system');
});

test('G3: query-identity endpoint rejects an invalid identity value (400)', async () => {
  // Note: creating a *system_only* Fabric connection runs an upfront live
  // connection test, which can't pass in this sandbox (1433 blocked), so the
  // system_only-rejection path is covered by the backend suite. Here we exercise
  // the endpoint's input validation on a real user_required connection.
  const created = await createConnection(`Fabric BadId ${Date.now()}`, 'user_required', ['oauth']);
  expect(created.ok()).toBeTruthy();
  const id = (await created.json()).id;
  const res = await api.patch(`/api/connections/${id}/query-identity`, { data: { query_identity: 'bogus' } });
  expect(res.status(), "invalid query_identity must be rejected").toBe(400);
});

test('G5 (UI): the identity toggle renders in the Knowledge Explorer connection modal', async ({ page }) => {
  // Ensure at least one user_required connection + a data source so it shows in the UI footer.
  const conn = await createConnection(`Fabric UI ${Date.now()}`, 'user_required', ['oauth']);
  expect(conn.ok()).toBeTruthy();
  const connId = (await conn.json()).id;
  const ds = await api.post('/api/data_sources', {
    data: { name: `Fabric UI DS ${Date.now()}`, connection_id: connId, is_public: true },
  });
  expect(ds.ok(), `POST /api/data_sources -> ${ds.status()}: ${await ds.text()}`).toBeTruthy();

  await page.goto('/instructions', { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle').catch(() => {});
  // Open the connections list ("View all") and pick a Fabric connection.
  const viewAll = page.getByRole('button', { name: /view all/i });
  await expect(viewAll.first()).toBeVisible({ timeout: 20000 });
  await viewAll.first().click();
  await page.getByRole('button', { name: /fabric/i }).first().click();

  // The ConnectionDetailModal shows "Service account" and "Me" identity buttons.
  await expect(page.getByRole('button', { name: /service account/i })).toBeVisible({ timeout: 15000 });
  await page.screenshot({ path: 'tests/entra/.auth/g5-identity-toggle.png', fullPage: true });
  await expect(page.getByRole('button', { name: /^\s*me\s*$/i })).toBeVisible({ timeout: 15000 });
});
