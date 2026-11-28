// Screenshot the AI Settings "Sandbox" section (UI evidence for the
// "LLM functions in code" setting; docs/feedback-loops/sandbox-llm-functions.md).
//
//   node tools/agent/sandbox_llm_settings_shot.mjs <out.png> [locale]
//
// Env: BOW_EMAIL, BOW_PASSWORD, BOW_ORIGIN (default http://localhost:3000).
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
const requireFromFrontend = createRequire(new URL('../../frontend/package.json', import.meta.url));
const { chromium } = requireFromFrontend('@playwright/test');

const [out, locale = 'en'] = process.argv.slice(2);
if (!out) { console.error('usage: node sandbox_llm_settings_shot.mjs <out.png> [locale]'); process.exit(2); }
const origin = process.env.BOW_ORIGIN || 'http://localhost:3000';
mkdirSync(dirname(out), { recursive: true });

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
await context.addInitScript((loc) => { try { localStorage.setItem('bow.locale', loc) } catch {} }, locale);
const page = await context.newPage();
await page.goto(`${origin}/users/sign-in`, { waitUntil: 'networkidle' }).catch(() => {});
await page.fill('#email', process.env.BOW_EMAIL || 'admin@example.com');
await page.fill('#password', process.env.BOW_PASSWORD || 'Password123!');
await Promise.all([
  page.waitForNavigation({ waitUntil: 'networkidle' }).catch(() => {}),
  page.click('button[type=submit]'),
]);
await page.goto(`${origin}/settings/ai_settings`, { waitUntil: 'networkidle' }).catch(() => {});
const section = page.locator('[data-testid="ai-section-sandbox"]');
await section.waitFor({ timeout: 30000 });
const toggle = section.locator('button[aria-expanded]').first();
if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
await page.waitForTimeout(800);
await section.scrollIntoViewIfNeeded();
await section.screenshot({ path: out });
console.log('saved', out);
await browser.close();
