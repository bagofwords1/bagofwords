// Reproduction driver for the two reported UI issues (Hebrew / RTL).
// Run from frontend/:  node .repro/repro.mjs <instructionA> <instructionB> <outDir>
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const [insA, insB, outDir = '.repro/shots'] = process.argv.slice(2);
if (!insA || !insB) { console.error('usage: node .repro/repro.mjs <insA> <insB> [outDir]'); process.exit(2); }
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
const page = await context.newPage();
// Hebrew locale -> <html dir="rtl"> (plugins/i18n.ts reads bow.locale)
await page.addInitScript(() => localStorage.setItem('bow.locale', 'he'));

// visual cursor marker so screenshots show where the pointer is
async function showCursor(x, y) {
  await page.evaluate(([x, y]) => {
    let d = document.getElementById('__cursor');
    if (!d) {
      d = document.createElement('div');
      d.id = '__cursor';
      d.style.cssText = 'position:fixed;width:14px;height:14px;border-radius:50%;background:rgba(220,38,38,.85);border:2px solid white;box-shadow:0 0 0 2px rgba(220,38,38,.5);z-index:99999;pointer-events:none;transform:translate(-50%,-50%)';
      document.body.appendChild(d);
    }
    d.style.left = x + 'px';
    d.style.top = y + 'px';
  }, [x, y]);
}

// ── login ────────────────────────────────────────────────────────────────────
await page.goto('http://localhost:3000/users/sign-in');
await page.waitForSelector('#email', { timeout: 30000 });
await page.fill('#email', 'admin@example.com');
await page.fill('#password', 'Password123!');
await page.click('button[type="submit"]');
await page.waitForURL((u) => !u.pathname.includes('/users/sign-in'), { timeout: 30000 });

const dir = await page.evaluate(() => document.documentElement.getAttribute('dir'));
console.log('html dir =', dir);

// dismiss the first-run product tour if it shows up
async function dismissTour() {
  const skip = page.getByText('דלג על ההדרכה', { exact: false }).first();
  if (await skip.isVisible({ timeout: 2500 }).catch(() => false)) {
    await skip.click().catch(() => {});
    await page.waitForTimeout(600);
  }
}
await dismissTour();

// ── issue 1: agent chip shows UUID ───────────────────────────────────────────
await page.goto(`http://localhost:3000/agents/instructions/${insA}`);
await page.waitForLoadState('networkidle').catch(() => {});
await page.waitForTimeout(3000);
await dismissTour();
// the agents KSelect trigger chip always contains the visible agent name
const agentChip = page.locator('button', { hasText: 'מכירות' }).last();
await agentChip.waitFor({ timeout: 15000 }).catch(() => {});
const chipText = await agentChip.innerText().catch(() => '(chip not found)');
const hasUuid = /[0-9a-f]{8}-[0-9a-f]{4}/.test(chipText);
console.log('issue1: chip text =', JSON.stringify(chipText));
console.log('issue1:', hasUuid ? 'FAIL — chip shows a raw UUID' : 'PASS — no raw UUID in chip');
await page.screenshot({ path: `${outDir}/issue1-1-chip.png` });
await agentChip.click();
await page.waitForTimeout(800);
const panelTxt = await page.evaluate(() => {
  const els = Array.from(document.querySelectorAll('div')).filter((d) => d.textContent.includes('Clear') && d.offsetHeight < 400 && d.offsetHeight > 0);
  return els.length ? els[els.length - 1].innerText.replace(/\n/g, ' | ') : '(no dropdown panel found)';
});
console.log('issue1: dropdown panel =', panelTxt);
console.log('issue1: hidden agent listed in dropdown =', panelTxt.includes('Legacy DWH') ? 'yes (PASS)' : 'no (FAIL)');
await page.screenshot({ path: `${outDir}/issue1-2-chip-dropdown.png` });
const box = await agentChip.boundingBox();
if (box) {
  await page.screenshot({
    path: `${outDir}/issue1-3-zoom.png`,
    clip: { x: Math.max(0, box.x - 320), y: Math.max(0, box.y - 260), width: 760, height: 380 },
  });
}
await page.keyboard.press('Escape').catch(() => {});

// ── issue 2: hover popover far from the change + disappears en route ─────────
await page.goto(`http://localhost:3000/agents/instructions/${insB}`);
await page.waitForLoadState('networkidle').catch(() => {});
await page.waitForTimeout(3000);
await dismissTour();
await page.waitForSelector('[id^="htc-"]', { timeout: 20000 }).catch(() => {});
await page.screenshot({ path: `${outDir}/issue2-0-review.png` });

// pick the hunk that wraps the most lines (most client rects)
const hunks = await page.evaluate(() => {
  return Array.from(document.querySelectorAll('[id^="htc-"]')).map((el) => {
    const rects = Array.from(el.getClientRects()).filter((r) => r.width > 2 && r.height > 2);
    return { id: el.id, rects: rects.map((r) => ({ x: r.x, y: r.y, w: r.width, h: r.height })) };
  });
});
console.log('issue2: hunks =', hunks.map((h) => `${h.id}:${h.rects.length} rects`).join(', '));
const multi = hunks.filter((h) => h.rects.length > 1).sort((a, b) => b.rects.length - a.rects.length)[0] || hunks[0];
if (!multi) { console.error('no hunks found'); process.exit(1); }

// where does the popover land when hovering the FIRST fragment? (for comparison)
const first = multi.rects[0];
await page.mouse.move(first.x + first.w / 2, first.y + first.h / 2);
await page.waitForTimeout(400);
await showCursor(first.x + first.w / 2, first.y + first.h / 2);
await page.screenshot({ path: `${outDir}/issue2-1a-hover-first-line.png` });

// hover the LAST line fragment of the hunk — where a user reading the change
// down the paragraph would put the mouse
const last = multi.rects[multi.rects.length - 1];
const hx = last.x + last.w / 2;
const hy = last.y + last.h / 2;
await page.mouse.move(hx, hy);
await page.waitForTimeout(400);
await showCursor(hx, hy);

// popover lookup — supports both the old per-hunk card (span.z-30 inside the
// hunk, visibility-toggled) and the new shared floating card (#htc-hover-card).
const getPop = () => page.evaluate((id) => {
  const shared = document.getElementById('htc-hover-card');
  if (shared) {
    const r = shared.getBoundingClientRect();
    return { visibility: 'visible', x: r.x, y: r.y, w: r.width, h: r.height, kind: 'shared' };
  }
  const el = document.getElementById(id);
  const p = el && el.querySelector('span.z-30');
  if (!p) return null;
  const st = getComputedStyle(p);
  const card = p.querySelector('span');
  const r = (card || p).getBoundingClientRect();
  return { visibility: st.visibility, x: r.x, y: r.y, w: r.width, h: r.height, kind: 'per-hunk' };
}, multi.id);
const pop = await getPop();
console.log('issue2: hover point =', { hx: Math.round(hx), hy: Math.round(hy) });
console.log('issue2: popover =', pop);
if (pop) {
  const px = pop.x + pop.w / 2;
  const py = pop.y + pop.h / 2;
  const dist = Math.round(Math.hypot(px - hx, py - hy));
  console.log(`issue2: distance from hover point to popover center = ${dist}px`);
  await page.screenshot({ path: `${outDir}/issue2-1-hover-far-popover.png` });

  // annotate: line from cursor to popover
  await page.evaluate(([x1, y1, x2, y2]) => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.id = '__line';
    svg.style.cssText = 'position:fixed;inset:0;width:100vw;height:100vh;z-index:99998;pointer-events:none';
    svg.innerHTML = `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="rgba(220,38,38,.7)" stroke-width="2" stroke-dasharray="6 4"/>`;
    document.body.appendChild(svg);
  }, [hx, hy, px, py]);
  await page.screenshot({ path: `${outDir}/issue2-2-annotated.png` });
  await page.evaluate(() => document.getElementById('__line')?.remove());

  // now walk the mouse toward the popover and record where it disappears
  const steps = 14;
  let vanishedAt = null;
  for (let i = 1; i <= steps; i++) {
    const mx = hx + ((px - hx) * i) / steps;
    const my = hy + ((py - hy) * i) / steps;
    await page.mouse.move(mx, my);
    await page.waitForTimeout(120);
    const vis = await page.evaluate((id) => {
      const shared = document.getElementById('htc-hover-card');
      if (shared) return 'visible';
      const el = document.getElementById(id);
      const p = el && el.querySelector('span.z-30');
      return p ? getComputedStyle(p).visibility : 'gone';
    }, multi.id);
    if (vis !== 'visible' && vanishedAt === null) {
      vanishedAt = i;
      await showCursor(mx, my);
      await page.screenshot({ path: `${outDir}/issue2-3-vanished-en-route.png` });
      console.log(`issue2: popover VANISHED at step ${i}/${steps} (mouse ${Math.round(mx)},${Math.round(my)}) before reaching it — FAIL`);
      break;
    }
  }
  if (vanishedAt === null) {
    // reached the card — hover its Accept button and confirm it is interactive
    await page.mouse.move(px, py);
    await page.waitForTimeout(300);
    const reachable = await page.evaluate((id) => {
      const shared = document.getElementById('htc-hover-card');
      if (shared) return !!shared.querySelector('button');
      const el = document.getElementById(id);
      const p = el && el.querySelector('span.z-30');
      return !!(p && getComputedStyle(p).visibility === 'visible');
    }, multi.id);
    await showCursor(px, py);
    await page.screenshot({ path: `${outDir}/issue2-4-reached-card.png` });
    console.log(`issue2: popover reachable = ${reachable ? 'yes — PASS' : 'no — FAIL'}`);
  }
} else {
  console.log('issue2: popover element not found under hunk');
}

await context.close();
await browser.close();
console.log('done');
