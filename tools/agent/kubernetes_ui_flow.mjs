// Drives the BOW UI through the Kubernetes connector's setup flow against the
// kind sandbox (tools/kubernetes/seed_kubernetes.sh) and captures evidence.
//
// NOTE: @playwright/test resolves from the script's own directory (ESM), so run
// from a copy inside frontend/ (e.g. frontend/.agent-tmp/) against a running
// stack. The access file path comes from the seed script's last line.
//
//   node kubernetes_ui_flow.mjs <outdir> </tmp/bow-agent/kubernetes-access.yaml>
//
// Steps captured:
//   01 catalog tile (Infrastructure chip)      02 connect form with the setup guide
//   03 a laptop kubeconfig (exec plugin) pasted → rejection message
//   04 the real access file pasted → Test connection result
//   05 schema discovery modal                   06/07 Select Tables (fixed + crd:: + metrics)
import { chromium } from '@playwright/test';
import { mkdirSync, readFileSync } from 'node:fs';

const [OUTDIR, ACCESS_FILE] = process.argv.slice(2);
const OUT = OUTDIR || '../media/pr/kubernetes-connector';
const BASE = process.env.BOW_ORIGIN || 'http://localhost:3000';
const EMAIL = process.env.BOW_EMAIL || 'admin@example.com';
const PASSWORD = process.env.BOW_PASSWORD || 'Password123!';
const EXEC = process.env.PW_CHROMIUM || undefined;
mkdirSync(OUT, { recursive: true });
const accessFile = readFileSync(ACCESS_FILE || '/tmp/bow-agent/kubernetes-access.yaml', 'utf8');

// What an admin might paste by mistake: a managed-cluster kubeconfig whose
// user authenticates through an exec credential plugin.
const LAPTOP_KUBECONFIG = `apiVersion: v1
kind: Config
clusters:
- name: prod-eks
  cluster:
    server: https://ABC123.gr7.eu-west-1.eks.amazonaws.com
    certificate-authority-data: LS0tLS1CRUdJTi0tLS0t
users:
- name: prod-eks
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: aws
      args: [eks, get-token, --cluster-name, prod]
contexts:
- name: prod-eks
  context: {cluster: prod-eks, user: prod-eks}
current-context: prod-eks
`;

const browser = await chromium.launch(EXEC ? { executablePath: EXEC } : {});
const ctx = await browser.newContext({ viewport: { width: 1512, height: 1000 } });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const shot = async (name, full = false) => {
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: full });
  console.log('shot', name);
};

// ── sign in ────────────────────────────────────────────────────────────────
await page.goto(`${BASE}/users/sign-in`, { waitUntil: 'commit' });
await page.locator('input[type=email]').first().fill(EMAIL);
await page.locator('input[type=password]').first().fill(PASSWORD);
await Promise.all([
  page.waitForURL((u) => !u.pathname.includes('sign-in'), { timeout: 20000 }).catch(() => {}),
  page.locator('button[type=submit]').first().click(),
]);
await page.waitForTimeout(2500);
try { await page.getByText(/skip onboarding/i).click({ timeout: 3000 }); await page.waitForTimeout(1500); } catch {}

// ── catalog → Kubernetes tile ──────────────────────────────────────────────
await page.goto(`${BASE}/agents/new`, { waitUntil: 'commit' });
await page.waitForTimeout(2500);
const portal = page.locator('#headlessui-portal-root');
if (!(await portal.count()) || !(await portal.innerText().catch(() => '')).trim()) {
  await page.getByText(/create new connection/i).first().click({ timeout: 15000 });
}
await page.waitForTimeout(1500);
try {
  const search = page.getByPlaceholder(/search/i).first();
  if (await search.count()) { await search.fill('kubernetes'); await page.waitForTimeout(600); }
} catch {}
await shot('01-catalog-kubernetes-tile');
const tile = page.getByRole('button', { name: /kubernetes/i }).first();
await tile.evaluate((el) => el.scrollIntoView({ block: 'center' })).catch(() => {});
await page.waitForTimeout(400);
await tile.click({ timeout: 8000 }).catch(async () => { await tile.click({ force: true }); });
await page.waitForTimeout(1500);
await shot('02-connect-form-setup-guide', true);

const guide = page.locator('[data-testid="setup-guide"]');
console.log('setup guide rendered:', await guide.count() > 0, '| steps:', await guide.locator('li').count());
const copyButtons = guide.getByRole('button', { name: /copy/i });
console.log('copy buttons:', await copyButtons.count());

const accessField = page.locator('textarea#access_file');
await accessField.waitFor({ timeout: 10000 });

// ── wrong paste first: a laptop kubeconfig with an exec plugin ─────────────
await accessField.fill(LAPTOP_KUBECONFIG);
await page.getByRole('button', { name: /test connection/i }).first().click({ timeout: 15000 });
await page.waitForTimeout(3000);
const rejection = await page.locator('body').innerText().then((t) => (t.match(/.*exec.*/i) || [''])[0]);
console.log('rejection message:', rejection.trim().slice(0, 200));
await shot('03-laptop-kubeconfig-rejected');

// ── the real access file ───────────────────────────────────────────────────
await accessField.fill(accessFile);
await page.getByRole('button', { name: /test connection/i }).first().click({ timeout: 15000 });
await page.getByText(/Connected successfully/i).first().waitFor({ timeout: 60000 }).catch(() => {});
const result = await page.locator('body').innerText().then((t) => (t.match(/Connected successfully[^\n]*/i) || [''])[0]);
console.log('test connection:', result.trim());
await shot('04-test-connection-ok');

// ── save → discovery → select tables ───────────────────────────────────────
await page.getByRole('button', { name: /save and continue/i }).first().click({ timeout: 15000 });
await page.getByText(/discovered \d+ tables/i).first().waitFor({ timeout: 180000 });
console.log('discovery:', await page.getByText(/discovered \d+ tables/i).first().innerText());
await shot('05-schema-discovery');
await page.getByRole('button', { name: /^connect$/i }).first().click({ timeout: 15000 });
await page.waitForTimeout(2500);
const agentName = page.getByPlaceholder(/sales, marketing/i).first();
if (await agentName.count()) await agentName.fill('Kubernetes (kind sandbox)');
await page.getByRole('button', { name: /save & continue|save and continue/i }).first().click({ timeout: 15000 }).catch(() => {});
for (let i = 0; i < 36; i++) {
  await page.waitForTimeout(5000);
  const t = await page.locator('body').innerText().catch(() => '');
  if (/crd::example\.com\/Widget|persistent_volumes/.test(t)) break;
}
console.log('url at tables step:', page.url());
await shot('06-select-tables');
await shot('07-select-tables-full', true);
const body = await page.locator('body').innerText();
for (const t of ['pods', 'containers', 'persistent_volumes', 'csi_drivers', 'ingress_classes', 'network_policies', 'crd::example.com/Widget', 'pod_metrics', 'logs']) {
  console.log(`table listed: ${t} -> ${body.includes(t)}`);
}
await browser.close();
