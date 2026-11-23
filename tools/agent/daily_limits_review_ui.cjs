// Review fixes 3 + 4, through the UI as admin.
const { chromium } = require('playwright');
const ORIGIN='http://localhost:3000', API='http://localhost:8000/api';
let failures = 0;
const check = (name, ok, detail) => { console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`, detail ?? ''); if (!ok) failures++; };
(async () => {
  const tok = (await (await fetch(`${API}/auth/jwt/login`, { method: 'POST', body: new URLSearchParams({ username: 'admin@example.com', password: 'Password123!' }) })).json()).access_token;
  const org = (await (await fetch(`${API}/organizations`, { headers: { Authorization: `Bearer ${tok}` } })).json())[0].id;
  const H = { Authorization: `Bearer ${tok}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' };
  const call = async (m, p, b) => { const r = await fetch(`${API}${p}`, { method: m, headers: H, body: b ? JSON.stringify(b) : undefined }); const t = await r.text(); return t ? JSON.parse(t) : null; };
  for (const p of await call('GET', `/organizations/${org}/usage-policies`)) if (['Precise', 'Precise renamed', 'Two connections'].includes(p.name)) await call('DELETE', `/organizations/${org}/usage-policies/${p.id}`);
  const precise = await call('POST', `/organizations/${org}/usage-policies`, { name: 'Precise', daily_spend_limit_usd: 0.004, daily_data_bytes_limit: 1024, monthly_data_bytes_limit: 1536 });

  const b = await chromium.launch(); const page = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  await page.goto(`${ORIGIN}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.getByLabel('Email').fill('admin@example.com'); await page.getByLabel('Password').fill('Password123!');
  await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}), page.click('button[type=submit]')]);
  await page.goto(`${ORIGIN}/settings/members?tab=quotas`, { waitUntil: 'networkidle' }).catch(() => {});
  await page.getByTestId('quota-limits').first().waitFor({ timeout: 30000 });

  // 4. Name-only edit keeps precise caps.
  await page.locator('tr', { hasText: 'Precise' }).getByRole('button', { name: /Edit/i }).click();
  await page.waitForTimeout(800);
  await page.getByPlaceholder('e.g. Analyst quota').fill('Precise renamed');
  await page.getByTestId('quota-save').click();
  await page.waitForTimeout(1500);
  const after = await call('GET', `/organizations/${org}/usage-policies/${precise.id}`);
  check('name-only edit keeps daily spend $0.004', after.daily_spend_limit_usd === 0.004, after.daily_spend_limit_usd);
  check('name-only edit keeps daily data 1,024 B', after.daily_data_bytes_limit === 1024, after.daily_data_bytes_limit);
  check('name-only edit keeps monthly data 1,536 B', after.monthly_data_bytes_limit === 1536, after.monthly_data_bytes_limit);

  // 3. 150 queries/day across connections next to 100/connection/month saves.
  await page.getByRole('button', { name: /New Quota/i }).click();
  await page.waitForTimeout(800);
  await page.getByPlaceholder('e.g. Analyst quota').fill('Two connections');
  await page.getByTestId('quota-queries-daily').fill('150');
  await page.getByTestId('quota-queries-monthly').fill('100');
  await page.waitForTimeout(300);
  const disabled = await page.getByTestId('quota-save').isDisabled();
  check('daily queries above per-connection monthly: Save enabled', !disabled);
  if (!disabled) { await page.getByTestId('quota-save').click(); await page.waitForTimeout(1500); }
  const two = (await call('GET', `/organizations/${org}/usage-policies`)).find(p => p.name === 'Two connections');
  check('daily queries above per-connection monthly: saved', !!two && two.daily_query_limit === 150 && two.monthly_query_limit === 100, two && [two.daily_query_limit, two.monthly_query_limit]);
  await b.close();
  console.log(failures ? `${failures} FAILED` : 'ALL PASSED');
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error('FAIL', e.message); process.exit(1); });
