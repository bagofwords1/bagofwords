// Loop (b): can a foreign page frame the app's artifact shell and run code in
// the app origin? And does a legitimate MCP host still render artifacts?
//
//   node run_framing.mjs <outDir> <attackerOrigin> <label>=<appOrigin>[,admin@email] ...
//
// For each app origin: the attacker page frames <app>/mcp-artifact-app.html and
// plays MCP host with forged artifact code; we report whether the code ran in
// the app origin (and could read an app-origin cookie). When an admin email is
// given, we also fetch ui://bagofwords/artifact through POST /api/mcp
// resources/read and render it in host.html (sandboxed srcdoc, as MCP hosts do).
import { chromium } from 'playwright';
import fs from 'node:fs';

const [outDir, attacker, ...targets] = process.argv.slice(2);
fs.mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined }).catch(() => chromium.launch());
const PW = 'Password123!';

async function resourceHtml(app, email) {
  const login = await fetch(`${app}/api/auth/jwt/login`, { method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username: email, password: PW }) });
  const { access_token } = await login.json();
  const who = await (await fetch(`${app}/api/users/whoami`, { headers: { authorization: `Bearer ${access_token}` } })).json();
  const r = await fetch(`${app}/api/mcp`, { method: 'POST', headers: { 'content-type': 'application/json',
    authorization: `Bearer ${access_token}`, 'x-organization-id': who.organizations[0].id },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'resources/read', params: { uri: 'ui://bagofwords/artifact' } }) });
  return (await r.json()).result.contents[0].text;
}

for (const spec of targets) {
  const [label, rest] = spec.split('=');
  const [app, email] = rest.split(',');
  const ctx = await browser.newContext({ viewport: { width: 700, height: 420 } });
  await ctx.addCookies([{ name: 'auth_token', value: 'demo-app-session', url: app }]);
  const page = await ctx.newPage();
  const consoleLines = [];
  page.on('console', (m) => consoleLines.push(`[${m.type()}] ${m.text()}`));
  const target = `${app}/mcp-artifact-app.html`;
  const resp = await ctx.request.get(target);
  const h = resp.headers();
  await page.goto(`${attacker}/attack.html?target=${encodeURIComponent(target)}`);
  await page.waitForTimeout(3000);
  const stolen = await page.evaluate(() => window.STOLEN || null);
  const frame = page.frames().find((f) => f !== page.mainFrame());
  let inFrame = null;
  try {
    inFrame = await frame.evaluate(() => ({ origin: location.origin, pwned: document.body.dataset.pwned || null,
      text: document.body.innerText.slice(0, 80) }));
  } catch (e) { inFrame = { error: String(e).split('\n')[0] }; }
  await page.screenshot({ path: `${outDir}/attack-${label}.png` });
  console.log(JSON.stringify({ label, target, csp: h['content-security-policy'] || null,
    xfo: h['x-frame-options'] || null, stolen, frame: inFrame,
    blocked: consoleLines.filter((l) => /frame|ancestor|X-Frame/i.test(l)) }));

  if (email) {
    const html = await resourceHtml(app, email);
    const host = await ctx.newPage();
    await host.goto(`${attacker}/host.html`);
    await host.evaluate((x) => window.mount(x), html);
    await host.waitForTimeout(4000);
    const f = host.frames().find((x) => x !== host.mainFrame());
    const r = await f.evaluate(() => ({ origin: location.origin,
      ok: (document.getElementById('ok') || {}).textContent || null,
      err: (document.querySelector('.artifact-error') || {}).textContent || null }));
    await host.screenshot({ path: `${outDir}/mcp-host-${label}.png` });
    console.log(JSON.stringify({ label: `${label}:mcp-host`, ...r }));
  }
  await ctx.close();
}
await browser.close();
