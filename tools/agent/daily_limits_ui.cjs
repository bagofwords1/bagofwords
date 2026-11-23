// UI verification for daily usage limits (BOW-224), driven as real users.
//   node tools/agent/daily_limits_ui.cjs <outdir>
// Needs: a running stack (boot_stack.sh --dev), admin@example.com, a member
// (seed_org.py --invite member@example.com), the "Interns" policy assigned to
// the member, org timezone Asia/Jerusalem and a working default model (Haiku or
// the stub). See docs/feedback-loops/2026-11-23-daily-usage-limits.md.
// Admin: list + create/edit with validation. Member: blocked chat, sidebar menu, profile.
const { chromium } = require('playwright');
const assert = require('assert');
const outdir = process.argv[2];
const ORIGIN = 'http://localhost:3000';
const API = 'http://localhost:8000/api';
const log = (...a) => console.log('[ui]', ...a);

async function login(page, email) {
  await page.goto(`${ORIGIN}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill('Password123!');
  await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}), page.click('button[type=submit]')]);
  await page.waitForTimeout(1500);
}

async function api(email) {
  const r = await fetch(`${API}/auth/jwt/login`, { method: 'POST', body: new URLSearchParams({ username: email, password: 'Password123!' }) });
  const tok = (await r.json()).access_token;
  const org = (await (await fetch(`${API}/organizations`, { headers: { Authorization: `Bearer ${tok}` } })).json())[0].id;
  const H = { Authorization: `Bearer ${tok}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' };
  const call = async (method, path, body) => {
    const res = await fetch(`${API}${path}`, { method, headers: H, body: body ? JSON.stringify(body) : undefined });
    const txt = await res.text(); return txt ? JSON.parse(txt) : null;
  };
  return { org, call };
}

async function openQuotas(page) {
  await page.goto(`${ORIGIN}/settings/members?tab=quotas`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForTimeout(1500);
  const tab = page.getByRole('link', { name: /^Quotas$/ });
  if (await tab.count()) { await tab.first().click().catch(() => {}); await page.waitForTimeout(1500); }
  await page.getByTestId('quota-limits').first().waitFor({ timeout: 20000 });
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const admin = await api('admin@example.com');
  // Re-runnable: drop the policy this flow creates.
  for (const p of await admin.call('GET', `/organizations/${admin.org}/usage-policies`)) {
    if (p.name === 'Contractors') await admin.call('DELETE', `/organizations/${admin.org}/usage-policies/${p.id}`);
    if (p.name === 'Interns') await admin.call('PUT', `/organizations/${admin.org}/usage-policies/${p.id}`, { daily_token_limit: 50000, daily_spend_limit_usd: 1, monthly_token_limit: 500000, monthly_spend_limit_usd: 10, daily_query_limit: 40 });
  }

  // ── Admin: list shows Day | Month per metric ─────────────────────────────
  await login(page, 'admin@example.com');
  await openQuotas(page);
  const interns = page.locator('tr', { hasText: 'Interns' }).getByTestId('quota-limits');
  const internsText = (await interns.innerText()).replace(/\s+/g, ' ');
  log('Interns limits cell:', internsText);
  assert.match(internsText, /Day Month Tokens 50,000 500,000/);
  assert.match(internsText, /Spend \$1\.00 \$10\.00/);
  const power = (await page.locator('tr', { hasText: 'Power users' }).getByTestId('quota-limits').innerText()).replace(/\s+/g, ' ');
  log('Power users limits cell:', power);
  assert.match(power, /Tokens — 20,000,000/);  // monthly only → day shows "—"
  await page.screenshot({ path: `${outdir}/after-quotas-list.png` });

  // ── Admin: create with daily > monthly → inline error, Save disabled ────
  await page.getByRole('button', { name: /New Quota/i }).click();
  await page.waitForTimeout(800);
  await page.getByPlaceholder('e.g. Analyst quota').fill('Contractors');
  await page.getByTestId('quota-tokens-daily').fill('600000');
  await page.getByTestId('quota-tokens-monthly').fill('500000');
  await page.getByText("Daily limit can't be higher than the monthly limit.").waitFor();
  assert.ok(await page.getByTestId('quota-save').isDisabled(), 'Save must be disabled while daily > monthly');
  const modalHeader = (await page.getByTestId('quota-limits-form').innerText()).replace(/\s+/g, ' ');
  log('modal header:', modalHeader.slice(0, 120));
  assert.match(modalHeader, /Per day Resets 00:00 Asia\/Jerusalem Per month Resets on the 1st/);
  await page.screenshot({ path: `${outdir}/after-quota-validation.png` });

  // Fix it: daily-only on queries, both on tokens and spend.
  await page.getByTestId('quota-tokens-daily').fill('20000');
  await page.getByTestId('quota-queries-daily').fill('25');
  await page.getByTestId('quota-spend-daily').fill('2.5');
  await page.getByTestId('quota-spend-monthly').fill('40');
  await page.getByTestId('quota-data-daily').fill('512');
  assert.ok(!(await page.getByTestId('quota-save').isDisabled()), 'Save enabled once valid');
  await page.screenshot({ path: `${outdir}/after-quota-create.png` });
  await page.getByTestId('quota-save').click();
  await page.getByText('Quota created').waitFor({ timeout: 10000 });
  await page.waitForTimeout(800);
  const created = (await admin.call('GET', `/organizations/${admin.org}/usage-policies`)).find(p => p.name === 'Contractors');
  log('saved via UI:', JSON.stringify({ d_tok: created.daily_token_limit, m_tok: created.monthly_token_limit, d_q: created.daily_query_limit, m_q: created.monthly_query_limit, d_b: created.daily_data_bytes_limit, d_s: created.daily_spend_limit_usd, m_s: created.monthly_spend_limit_usd }));
  assert.deepStrictEqual(
    [created.daily_token_limit, created.monthly_token_limit, created.daily_query_limit, created.monthly_query_limit, created.daily_data_bytes_limit, created.daily_spend_limit_usd, created.monthly_spend_limit_usd],
    [20000, 500000, 25, null, 512 * 1024 * 1024, 2.5, 40],
  );
  const contractors = (await page.locator('tr', { hasText: 'Contractors' }).getByTestId('quota-limits').innerText()).replace(/\s+/g, ' ');
  log('Contractors row:', contractors);
  assert.match(contractors, /Queries 25 —/);

  // Edit round-trip: the modal loads the stored daily values.
  await page.locator('tr', { hasText: 'Interns' }).getByRole('button', { name: /Edit/i }).click();
  await page.waitForTimeout(800);
  assert.strictEqual(await page.getByTestId('quota-tokens-daily').inputValue(), '50000');
  assert.strictEqual(await page.getByTestId('quota-spend-daily').inputValue(), '1');
  await page.screenshot({ path: `${outdir}/after-quota-edit.png` });
  await page.getByRole('button', { name: 'Cancel' }).click();

  // ── Member: one run, then the daily cap is reached ──────────────────────
  const member = await api('member@example.com');
  const mctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const mpage = await mctx.newPage();
  await login(mpage, 'member@example.com');

  async function ask(text) {
    await mpage.goto(`${ORIGIN}/`, { waitUntil: 'networkidle' }).catch(() => {});
    await mpage.waitForTimeout(1500);
    const box = mpage.locator('textarea, [contenteditable="true"]').first();
    await box.click();
    await box.fill(text);
    await mpage.keyboard.press('Enter');
    await mpage.waitForURL(/\/reports\//, { timeout: 30000 });
    await mpage.waitForTimeout(25000);
  }

  // Start the member's day clean of caps so the first run succeeds.
  const internsPre = (await admin.call('GET', `/organizations/${admin.org}/usage-policies`)).find(p => p.name === 'Interns');
  await admin.call('PUT', `/organizations/${admin.org}/usage-policies/${internsPre.id}`, { daily_token_limit: null, daily_spend_limit_usd: null, monthly_token_limit: null, monthly_spend_limit_usd: null, daily_query_limit: null });
  await ask('Show total order amount by region from orders_1');
  const whoami = await member.call('GET', '/users/whoami');
  const used = whoami.organizations.find(o => o.id === member.org).usage_quota.daily.tokens.used;
  log('member tokens used today after first run:', used);
  assert.ok(used > 0, 'first run records daily tokens');

  // Admin lowers Interns' daily token cap to exactly what the member used.
  const internsPolicy = (await admin.call('GET', `/organizations/${admin.org}/usage-policies`)).find(p => p.name === 'Interns');
  await admin.call('PUT', `/organizations/${admin.org}/usage-policies/${internsPolicy.id}`, { daily_token_limit: used, daily_spend_limit_usd: null });

  await ask('And by customer?');
  const blocked = mpage.getByText(/Daily LLM token quota exceeded\. Resets at 00:00 Asia\/Jerusalem/).first();
  await blocked.waitFor({ timeout: 30000 });
  log('member sees:', (await blocked.innerText()).slice(0, 200));
  await mpage.screenshot({ path: `${outdir}/after-member-blocked.png` });

  // Sidebar account menu: today's capped rows + the reset line.
  await mpage.reload({ waitUntil: 'networkidle' }).catch(() => {});
  await mpage.waitForTimeout(2000);
  await mpage.getByRole('button', { name: /member/i }).last().click();
  await mpage.waitForTimeout(800);
  const menu = mpage.getByText(/Tokens · today/).first();
  await menu.waitFor({ timeout: 10000 });
  await mpage.getByText(/Today resets 00:00 Asia\/Jerusalem/).waitFor();
  await mpage.screenshot({ path: `${outdir}/after-member-menu.png` });
  await mpage.keyboard.press('Escape');

  // Profile → Usage tab: "Today: used / limit" under tokens, in red when full.
  await mpage.getByRole('button', { name: /member/i }).last().click();
  await mpage.getByText('Account Settings').first().click();
  await mpage.waitForTimeout(1200);
  await mpage.getByRole('button', { name: /^Usage$/ }).or(mpage.getByText(/^Usage$/)).first().click();
  const today = mpage.getByTestId('usage-today-tokens');
  await today.waitFor({ timeout: 10000 });
  log('profile today line:', await today.innerText());
  assert.match(await today.innerText(), /^Today: [\d,]+ \/ [\d,]+$/);
  await mpage.screenshot({ path: `${outdir}/after-member-profile-usage.png` });

  // Restore the demo cap.
  await admin.call('PUT', `/organizations/${admin.org}/usage-policies/${internsPolicy.id}`, { daily_token_limit: 50000, daily_spend_limit_usd: 1, monthly_token_limit: 500000, monthly_spend_limit_usd: 10, daily_query_limit: 40 });
  log('ALL UI CHECKS PASSED');
  await browser.close();
})().catch(async e => { console.error('[ui] FAIL', e.message); process.exit(1); });
