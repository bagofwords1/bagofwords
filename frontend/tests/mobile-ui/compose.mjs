// Build labeled before/after comparison PNGs from the captured shots.
import { chromium } from '@playwright/test';
import fs from 'fs';

const EXE = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const ROOT = 'tests/mobile-ui/output';

const b64 = (p) => fs.readFileSync(p).toString('base64');
const pairs = [
  { file: 'cmp-artifact', title: 'Public artifact — top bar & title', before: 'before/03-public-artifact.png', after: 'after/03-public-artifact.png',
    beforeNote: 'Buttons overlap; tab labels collide; tab is UUID', afterNote: 'Icon-only bar, no overlap; tab shows report title' },
  { file: 'cmp-report', title: 'Report chat — prompt box & title', before: 'before/06-report-chat.png', after: 'after/06-report-chat.png',
    beforeNote: 'Loose padding; tab title is UUID', afterNote: 'Tighter prompt box; tab shows report title' },
  { file: 'cmp-dashboards', title: 'Dashboards — mobile navigation', before: 'before/07-dashboards.png', after: 'after/07-dashboards.png',
    beforeNote: 'No way to navigate on mobile', afterNote: 'Mobile top bar: menu · logo · new report' },
];

const browser = await chromium.launch({ headless: true, executablePath: EXE });
const page = await browser.newPage();
for (const p of pairs) {
  const html = `<!doctype html><html><body style="margin:0;background:#eef0f3;font-family:system-ui;padding:24px">
    <div style="font-size:20px;font-weight:700;margin-bottom:16px">${p.title}</div>
    <div style="display:flex;gap:24px">
      ${[['BEFORE', p.before, p.beforeNote, '#dc2626'], ['AFTER', p.after, p.afterNote, '#16a34a']].map(([lbl, img, note, col]) => `
        <div style="flex:1">
          <div style="font-weight:700;color:${col};margin-bottom:6px">${lbl}</div>
          <div style="font-size:13px;color:#555;margin-bottom:8px;height:34px">${note}</div>
          <img src="data:image/png;base64,${b64(`${ROOT}/${img}`)}" style="width:100%;border:1px solid #ccc;border-radius:8px;display:block"/>
        </div>`).join('')}
    </div>
  </body></html>`;
  await page.setViewportSize({ width: 900, height: 1000 });
  await page.setContent(html, { waitUntil: 'load' });
  await page.waitForTimeout(200);
  const el = await page.$('body');
  await el.screenshot({ path: `${ROOT}/${p.file}.png` });
  console.log('composed', p.file);
}
await browser.close();
