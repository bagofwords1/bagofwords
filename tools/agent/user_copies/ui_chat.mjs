// Ask the agent one question in the browser as a given member, and capture it.
//
// The report is opened through the API (it only pins which agent the chat is
// about); the question is typed and sent in the real chat box, and the answer
// is whatever the page renders once the run ends.
//
//   node tools/agent/user_copies/ui_chat.mjs --email demo1@example.com \
//     --agent <data_source_id> --prompt "How many sales rows..." --out media/pr/<slug>/chat-demo1.png
//
// Env: BOW_ORIGIN (http://localhost:3000), BOW_API (http://localhost:8000),
// BOW_MEMBER_PASSWORD (the local dev password by default).
// Prints the report id and the answer text; exits 1 if the run did not finish.
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const args = process.argv.slice(2);
const arg = (name, fallback = '') => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
const api = process.env.BOW_API || 'http://localhost:8000';
const password = process.env.BOW_MEMBER_PASSWORD || 'Password123!';
const email = arg('email');
const agent = arg('agent');
const prompt = arg('prompt');
const out = arg('out', 'chat.png');
if (!email || !agent || !prompt) {
  console.error('usage: ui_chat.mjs --email <member> --agent <data_source_id> --prompt "<question>" [--out file.png]');
  process.exit(2);
}

const form = new URLSearchParams({ username: email, password });
const token = (await (await fetch(`${api}/api/auth/jwt/login`, { method: 'POST', body: form })).json()).access_token;
const org = (await (await fetch(`${api}/api/organizations`, { headers: { Authorization: `Bearer ${token}` } })).json())[0].id;
const report = await (await fetch(`${api}/api/reports`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}`, 'X-Organization-Id': org, 'Content-Type': 'application/json' },
  body: JSON.stringify({ title: prompt.slice(0, 60), widget: null, files: [], data_sources: [agent] }),
})).json();

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
const page = await context.newPage();

await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', email);
await page.fill('#password', password);
await page.click('button[type=submit]');
await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 60000 });

await page.goto(`${origin}/reports/${report.id}`);
const box = page.locator('[contenteditable="true"]').first();
await box.waitFor({ timeout: 120000 });
await box.click();
await page.keyboard.type(prompt, { delay: 5 });
await page.locator('button[class*="rounded-full"]').last().click();

// Running: the stop button is on screen. Done: it is gone again.
const stop = page.locator('[data-testid="stop-button"]');
await stop.waitFor({ state: 'visible', timeout: 60000 }).catch(() => {});
let finished = true;
await stop.waitFor({ state: 'hidden', timeout: 600000 }).catch(() => { finished = false; });
await page.waitForTimeout(2500);
await page.screenshot({ path: out });
const text = (await page.locator('body').innerText()).replace(/\s+\n/g, '\n');
console.log(JSON.stringify({ report_id: report.id, email, finished, shot: out, answer_tail: text.slice(-700) }, null, 1));
await browser.close();
process.exit(finished ? 0 : 1);
