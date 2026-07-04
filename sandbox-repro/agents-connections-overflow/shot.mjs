import { chromium } from 'playwright';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = process.env.OUT_DIR || HERE;
const fileUrl = (w, n, fix) => 'file://' + path.join(HERE, `repro.html?w=${w}&n=${n}${fix ? '&fix=1' : ''}`);

const cases = [
  { name: 'fixed-w300-n8',  w: 300, n: 8,  fix: 1, note: 'FIXED: default width, 8 connections' },
  { name: 'fixed-w220-n8',  w: 220, n: 8,  fix: 1, note: 'FIXED: min width, 8 connections' },
  { name: 'fixed-w300-n3',  w: 300, n: 3,  fix: 1, note: 'FIXED: default width, 3 connections' },
  { name: 'fixed-w300-n12', w: 300, n: 12, fix: 1, note: 'FIXED: default width, 12 connections' },
  { name: 'repro-w300-n8',  w: 300, n: 8,  fix: 0, note: 'BEFORE: default width, 8 connections (bug)' },
  { name: 'repro-w220-n8',  w: 220, n: 8,  fix: 0, note: 'BEFORE: min width, 8 connections (worse)' },
];

// CHROME_BIN lets you point at a pre-installed Chromium (e.g. the sandbox's
// /opt/pw-browsers/chromium-1194/chrome-linux/chrome). Omit to let Playwright
// resolve its own download.
const browser = await chromium.launch(
  process.env.CHROME_BIN ? { executablePath: process.env.CHROME_BIN } : {}
);
const page = await browser.newPage({ viewport: { width: 900, height: 160 }, deviceScaleFactor: 2 });

const results = [];
for (const c of cases) {
  await page.goto(fileUrl(c.w, c.n, c.fix), { waitUntil: 'networkidle' });
  // Wait until Tailwind JIT has actually applied flex to the footer.
  await page.waitForFunction(() => {
    const f = document.querySelector('aside > .border-t');
    return f && getComputedStyle(f).display === 'flex';
  }, { timeout: 15000 });
  const m = await page.evaluate(() => window.__measure());
  if (m.footerDisplay !== 'flex') throw new Error('Tailwind not applied: ' + JSON.stringify(m));
  results.push({ ...c, ...m });
  await page.screenshot({ path: path.join(OUT, c.name + '.png') });
  console.log(`${c.name.padEnd(16)} | overflow=${String(m.overflowPx).padStart(4)}px | viewAllSpill=${m.viewAllSpillPx}px | ${c.note}`);
}

await browser.close();
console.log('\nJSON:\n' + JSON.stringify(results, null, 2));
