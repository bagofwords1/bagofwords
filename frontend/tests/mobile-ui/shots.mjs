// Mobile screenshot sweep. Usage: PHASE=before REPORT_ID=... node tests/mobile-ui/shots.mjs
import { chromium, devices } from '@playwright/test';
import fs from 'fs';

const EXE = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const BASE = process.env.BASE || 'http://localhost:3000';
const PHASE = process.env.PHASE || 'before';
const ROOT = process.env.OUT || 'tests/mobile-ui/output';
const OUT = `${ROOT}/${PHASE}`;
const REPORT_ID = process.env.REPORT_ID || JSON.parse(fs.readFileSync(`${ROOT}/report.json`, 'utf8')).report_id;
fs.mkdirSync(OUT, { recursive: true });

// iPhone 12/13 metrics
const VIEWPORT = { width: 390, height: 844 };
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1';

const ROUTES = [
  { label: '01-login',        path: '/users/sign-in',            auth: false, wait: '#email' },
  { label: '02-signup',       path: '/users/sign-up',            auth: false, wait: '#email' },
  { label: '03-public-artifact', path: `/r/${REPORT_ID}`,        auth: false, settle: 6000 },
  { label: '04-home',         path: '/',                          auth: true,  settle: 2500 },
  { label: '05-reports-list', path: '/reports',                   auth: true,  settle: 2500 },
  { label: '06-report-chat',  path: `/reports/${REPORT_ID}`,      auth: true,  settle: 5000 },
  { label: '07-dashboards',   path: '/dashboards',                auth: true,  settle: 2000 },
  { label: '08-instructions', path: '/instructions',              auth: true,  settle: 2000 },
  { label: '09-queries',      path: '/queries',                   auth: true,  settle: 2000 },
  { label: '10-settings',     path: '/settings/overview',         auth: true,  settle: 2000 },
  { label: '11-monitoring',   path: '/monitoring',                auth: true,  settle: 2000 },
];

const browser = await chromium.launch({ headless: true, executablePath: EXE });
const authState = `${ROOT}/state.json`;

async function makeCtx(auth) {
  return browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    userAgent: UA,
    ...(auth ? { storageState: authState } : {}),
  });
}

const metrics = [];
for (const r of ROUTES) {
  const ctx = await makeCtx(r.auth);
  const page = await ctx.newPage();
  try {
    await page.goto(`${BASE}${r.path}`, { waitUntil: 'commit', timeout: 90000 });
    if (r.wait) await page.waitForSelector(r.wait, { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(r.settle || 1200);
    // Measure vertical overflow (body scroll beyond viewport)
    const m = await page.evaluate(() => ({
      scrollH: document.documentElement.scrollHeight,
      innerH: window.innerHeight,
      bodyH: document.body.scrollHeight,
      overflowX: document.documentElement.scrollWidth > window.innerWidth,
      title: document.title,
    }));
    await page.screenshot({ path: `${OUT}/${r.label}.png`, fullPage: false });
    const verticalScroll = m.scrollH > m.innerH + 2;
    metrics.push({ label: r.label, path: r.path, verticalScroll, overflowX: m.overflowX, scrollH: m.scrollH, innerH: m.innerH, title: m.title });
    console.log(`${r.label.padEnd(20)} vScroll=${verticalScroll?'YES':'no '} xOverflow=${m.overflowX?'YES':'no '} scrollH=${m.scrollH} innerH=${m.innerH} title="${m.title}"`);
  } catch (e) {
    console.log(`${r.label.padEnd(20)} ERROR ${e.message.split('\n')[0]}`);
    metrics.push({ label: r.label, path: r.path, error: e.message.split('\n')[0] });
  }
  await ctx.close();
}
fs.writeFileSync(`${OUT}/metrics.json`, JSON.stringify(metrics, null, 2));
console.log('DONE', PHASE);
await browser.close();
