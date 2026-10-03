// Render installed AG Grid with the wrappers' actual CSS and typography token.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../..');
const { chromium } = require(path.join(root, 'frontend/node_modules/playwright'));
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const files = ['frontend/components/RenderTable.vue', 'frontend/components/dashboard/table/TableAgGrid.vue'];
const sources = files.map(read);
const css = [...sources, read('frontend/components/AgGridComponent.vue')].map(s => s.match(/<style[^>]*>([\s\S]*?)<\/style>/)[1]).join('\n');
const label = process.argv[2] || 'after';
if (!['before', 'after'].includes(label)) throw new Error('Use before or after');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 680 }, deviceScaleFactor: 1 });
    for (const dark of [false, true]) {
      const bg = dark ? '#111827' : '#ffffff';
      const fg = dark ? '#f9fafb' : '#111827';
      const border = dark ? '#374151' : '#e5e7eb';
      await page.setContent(`<body style="margin:32px;background:${bg};color:${fg};font-family:Arial">${sources.map((s, i) => `<section style="margin-bottom:28px"><h3>${i ? 'Dashboard table' : 'Query results'} · Genre options</h3><div class="ag-grid-themed ag-theme-custom" style="height:230px;--ag-background-color:${bg};--ag-foreground-color:${fg};--ag-header-background-color:${bg};--ag-header-foreground-color:${fg};--ag-border-color:${border};--ag-font-family:Arial;--ag-font-size:${s.match(/'--ag-font-size': '([^']+)'/)[1]};--ag-row-hover-color:#60a5fa20;--ag-selected-row-background-color:#60a5fa30;--ag-odd-row-background-color:${bg}"><div class="grid-container"><div id="grid${i}" class="ag-grid ag-theme-balham${dark ? '-dark' : ''}"></div></div></div></section>`).join('')}</body>`);
      for (const file of ['ag-grid.css', 'ag-theme-balham.css']) await page.addStyleTag({ path: path.join(root, 'frontend/node_modules/ag-grid-community/styles', file) });
      await page.addStyleTag({ content: css });
      await page.addScriptTag({ path: path.join(root, 'frontend/node_modules/ag-grid-community/dist/ag-grid-community.min.noStyle.js') });
      await page.evaluate(() => {
        for (const id of ['grid0', 'grid1']) agGrid.createGrid(document.getElementById(id), {
          columnDefs: [{ field: 'GenreId' }, { field: 'GenreName' }],
          defaultColDef: { flex: 1, minWidth: 110, sortable: true, resizable: true },
          rowData: ['Alternative', 'Alternative & Punk', 'Blues', 'Bossa Nova', 'Classical', 'Comedy'].map((name, i) => ({ GenreId: i + 1, GenreName: name })),
        });
      });
      await page.locator('#grid1 .ag-cell').first().waitFor();
      results.push(...await page.evaluate(dark => ['grid0', 'grid1'].map(id => {
        const grid = document.getElementById(id);
        const cell = getComputedStyle(grid.querySelector('.ag-cell'));
        return { dark, id, font: cell.fontSize, rowHeight: grid.querySelector('.ag-row').getBoundingClientRect().height, headerHeight: grid.querySelector('.ag-header').getBoundingClientRect().height, foreground: cell.color, background: getComputedStyle(grid.querySelector('.ag-row')).backgroundColor };
      }), dark));
      await page.screenshot({ path: path.join(root, `media/pr/aggrid-typography/${label}-${dark ? 'dark' : 'light'}.png`) });
    }
    console.log(JSON.stringify(results, null, 2));
    for (const result of results) {
      assert.equal(result.font, '12px', `${result.id} dark=${result.dark}: readable type`);
      assert.equal(result.rowHeight, 28);
      assert.equal(result.headerHeight, 33); // 32px header plus its bottom border.
      assert.equal(result.background, result.dark ? 'rgb(17, 24, 39)' : 'rgb(255, 255, 255)');
      assert.equal(result.foreground, result.dark ? 'rgb(249, 250, 251)' : 'rgb(17, 24, 39)');
    }
    console.log('PASS: both wrappers, light/dark, readable font, compact rows, theme colors');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
