import { test, expect } from '../fixtures/feature-test';

// Regression: an instruction must survive being displayed AND being edited
// without losing markdown constructs.
//
// Two separate failures used to compound here.
//
// 1. Display. InstructionText styled only h1-h3, p, lists, code and quotes, so
//    every other element markdown-it emits met Tailwind's preflight bare:
//    tables collapsed to borderless zero-padding cells that read as one run of
//    text, links were coloured exactly like body copy, <hr> painted nothing,
//    and h4-h6 rendered at body size and weight. RTL tables also inherited the
//    document's LTR and laid their columns out backwards.
//
// 2. Storage. The editor registered only StarterKit + Mention, so tables,
//    links, images and (via a missing serializer case) thematic rules had no
//    schema node. ProseMirror dropped them on parse and docToMarkdown wrote
//    the flattened document back — one unsupported construct could strip every
//    table out of an instruction permanently on the next save.
//
// The fixture below is deliberately mundane; what matters is that it contains
// one of each construct, in both text directions.
const MD = [
  '# Depot Handbook',
  '',
  'Covers **scheduling**, _billing_ and ~~retired~~ current routes.',
  '',
  '## Fact tables',
  '',
  '| Table | Grain | Measure |',
  '| --- | --- | --- |',
  '| `fct_trip` | one row per trip | `distance_km` |',
  '| `fct_charge` | one row per charge | `amount_cents` |',
  '',
  '---',
  '',
  '### Rules',
  '',
  '1. Filter `fct_trip` on `is_void = false`.',
  '2. Rollups run in three steps:',
  '   - per-hour bucket',
  '   - per-depot bucket',
  '     - flag under 10%',
  '     - flag over 90%',
  '   - weekly summary',
  '3. Never join two fact tables.',
  '',
  '#### Example',
  '',
  '```sql',
  'SELECT depot_id, COUNT(*) FROM fct_trip GROUP BY 1',
  '```',
  '',
  '##### Escalation',
  '',
  '> Ask the platform team first.',
  '> They own the contract tests, which cover:',
  '> - nightly reconciliation',
  '> - depot alerting',
  '',
  '###### Retired',
  '',
  'See the [migration notes](https://example.com/depot/migration).',
  '',
  '## נהלים בעברית',
  '',
  '| מדד | טבלה |',
  '| --- | --- |',
  '| נסיעות | `fct_trip` |',
  '| חיובים | `fct_charge` |',
  '',
  '![depot legend](https://example.com/depot/legend.png)',
  '',
].join('\n');

// Counting constructs rather than diffing text: an edit legitimately changes
// characters, but it must never reduce the number of tables, links or rules.
const PROBES = {
  tableRows: /^[^\S\n]*\|.*\|[^\S\n]*$/gm,
  thematicBreaks: /^-{3}$/gm,
  links: /\[[^\]]*\]\([^)]*\)/g,
  images: /!\[[^\]]*\]\([^)]*\)/g,
  codeFences: /^```/gm,
  nestedListItems: /^ {2,}- /gm,
  headings: /^#{1,6} /gm,
  quotedLines: /^> /gm,
};
const profile = (text: string) =>
  Object.fromEntries(Object.entries(PROBES).map(([k, re]) => [k, (text.match(re) || []).length]));

test('an instruction renders, edits and saves without losing markdown', async ({ page }) => {
  const cookies = await page.context().cookies();
  const rawToken = cookies.find((c) => c.name === 'auth.token' || c.name === 'auth_token')?.value;
  expect(rawToken, 'auth token cookie should exist in the admin storage state').toBeTruthy();
  const bearer = decodeURIComponent(rawToken!);
  const auth = bearer.startsWith('Bearer') ? bearer : `Bearer ${bearer}`;

  const whoami = await page.request.get('/api/users/whoami', { headers: { Authorization: auth } });
  expect(whoami.ok()).toBeTruthy();
  const orgId = (await whoami.json()).organizations?.[0]?.id;
  expect(orgId, 'admin user should belong to an organization').toBeTruthy();
  const headers = { Authorization: auth, 'X-Organization-Id': orgId };

  const created = await page.request.post('/api/instructions', {
    headers,
    data: {
      title: 'Depot Handbook',
      text: MD,
      status: 'published',
      category: 'general',
      load_mode: 'always',
      data_source_ids: [],
    },
  });
  expect(created.ok(), `create instruction failed: ${created.status()}`).toBeTruthy();
  const instruction = await created.json();

  await page.goto(`/agents/instructions/${instruction.id}`, { waitUntil: 'commit' });
  await expect(page.locator('.instruction-prose').first()).toBeVisible({ timeout: 45000 });
  await expect(page.locator('.instruction-prose').first()).toContainText('Depot Handbook', { timeout: 30000 });

  // ── 1. Everything markdown-it emits is rendered AND styled ────────────────
  const view = await page.evaluate(() => {
    const el = document.querySelector('.instruction-prose')!;
    const px = (v: string | undefined) => parseFloat(v || '0');
    const cell = el.querySelector('td');
    const cellStyle = cell && getComputedStyle(cell);
    const link = el.querySelector('a');
    const body = el.querySelector('p');
    const rule = el.querySelector('hr');
    const h4 = el.querySelector('h4');
    const rtlTable = [...el.querySelectorAll('table')].find((t) => t.getAttribute('dir') === 'rtl');
    return {
      tables: el.querySelectorAll('table').length,
      tableScrollWrappers: el.querySelectorAll('.md-table-wrap').length,
      cellBorderPx: px(cellStyle?.borderTopWidth),
      cellPaddingPx: px(cellStyle?.paddingLeft),
      linkColor: link && getComputedStyle(link).color,
      bodyColor: body && getComputedStyle(body).color,
      linkUnderlined: link ? getComputedStyle(link).textDecorationLine.includes('underline') : false,
      ruleBorderPx: px(rule ? getComputedStyle(rule).borderTopWidth : '0'),
      h4Px: px(h4 ? getComputedStyle(h4).fontSize : '0'),
      h4Weight: h4 ? getComputedStyle(h4).fontWeight : '',
      bodyPx: px(body ? getComputedStyle(body).fontSize : '0'),
      headingTags: [...el.querySelectorAll('h1,h2,h3,h4,h5,h6')].map((h) => h.tagName).join(','),
      nestedLists: el.querySelectorAll('li > ul, li > ol').length,
      images: el.querySelectorAll('img').length,
      rtlTableDirection: rtlTable ? getComputedStyle(rtlTable).direction : null,
      scripts: el.querySelectorAll('script').length,
    };
  });

  expect(view.tables, 'both tables render as real tables').toBe(2);
  expect(view.tableScrollWrappers, 'each table gets its own scroll container').toBe(2);
  expect(view.cellBorderPx, 'cells are separated by a visible border').toBeGreaterThan(0);
  expect(view.cellPaddingPx, 'cells have a gutter so text does not run together').toBeGreaterThan(0);
  expect(view.linkUnderlined, 'links are underlined').toBe(true);
  expect(view.linkColor, 'links are not the same colour as body text').not.toBe(view.bodyColor);
  expect(view.ruleBorderPx, 'a thematic break paints a line').toBeGreaterThan(0);
  expect(view.h4Weight, 'h4 is bolder than body text').toBe('600');
  expect(view.headingTags, 'all six heading levels survive').toBe('H1,H2,H3,H4,H5,H6,H2');
  expect(view.nestedLists, 'nested lists stay nested').toBeGreaterThanOrEqual(2);
  expect(view.images).toBe(1);
  expect(view.rtlTableDirection, 'an RTL table lays its columns out right-to-left').toBe('rtl');
  expect(view.scripts, 'rendered markdown is sanitized before v-html').toBe(0);

  // ── 2. Editing mounts the regular editor, with the same content ───────────
  await page.getByRole('button', { name: 'Edit', exact: true }).first().click();
  await expect(page.locator('.tiptap-prose').first()).toBeVisible({ timeout: 30000 });

  const editor = await page.evaluate(() => {
    const el = document.querySelector('.tiptap-prose')!;
    return {
      tables: el.querySelectorAll('table').length,
      links: el.querySelectorAll('a').length,
      rules: el.querySelectorAll('hr').length,
      // ProseMirror inserts its own <img class="ProseMirror-separator"> around
      // inline nodes — count only images that carry a src.
      images: el.querySelectorAll('img[src]:not(.ProseMirror-separator)').length,
      nestedLists: el.querySelectorAll('li > ul, li > ol').length,
      headingTags: [...el.querySelectorAll('h1,h2,h3,h4,h5,h6')].map((h) => h.tagName).join(','),
      // The dir decoration lands on tiptap's table wrapper, so read the
      // direction the browser resolves rather than the attribute.
      tableDirections: [...el.querySelectorAll('table')].map((t) => getComputedStyle(t).direction),
    };
  });
  expect(editor.tables, 'tables reach the editor schema').toBe(2);
  expect(editor.links, 'links reach the editor schema').toBe(1);
  expect(editor.rules, 'thematic breaks reach the editor schema').toBe(1);
  expect(editor.images, 'images reach the editor schema').toBe(1);
  expect(editor.nestedLists, 'nested lists reach the editor schema').toBeGreaterThanOrEqual(2);
  expect(editor.headingTags, 'h4-h6 are in the editor schema, not demoted to paragraphs')
    .toBe('H1,H2,H3,H4,H5,H6,H2');
  // The editor and the read-only view must agree on direction per table, or an
  // RTL table looks mirrored the moment you click Edit.
  expect(editor.tableDirections, 'the editor resolves table direction the same way the reader does')
    .toEqual(['ltr', 'rtl']);

  // ── 3. Saving an edit keeps every construct in the STORED markdown ────────
  const before = (await (await page.request.get(`/api/instructions/${instruction.id}`, { headers })).json()).text;

  const marker = `reviewed-${Date.now()}`;
  await page.locator('.tiptap-prose p').first().click();
  await page.keyboard.press('End');
  await page.keyboard.type(` Marker ${marker}.`);
  await page.getByRole('button', { name: /^Save$/ }).first().click();
  await expect(page.locator('.instruction-prose').first()).toBeVisible({ timeout: 30000 });

  const after = (await (await page.request.get(`/api/instructions/${instruction.id}`, { headers })).json()).text;
  expect(after, 'the typed edit reached storage').toContain(marker);
  expect(profile(after), 'no markdown construct was dropped by the save').toEqual(profile(before));
});
