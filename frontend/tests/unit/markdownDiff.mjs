// Markdown-aware tracked changes for instructions (utils/markdownDiff.ts).
// A diff must render like the instruction it changes — headings, lists,
// tables and code — and decide each block's direction by its words, not by
// its first letter, with deleted words left out of the vote.
//
// Run: cd frontend && node tests/unit/markdownDiff.mjs
import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'

// utils/ modules import their siblings without an extension (Vite resolves
// them); resolve those to the .ts file here.
registerHooks({
  resolve(specifier, context, next) {
    try { return next(specifier, context) } catch (err) {
      if (specifier.startsWith('.')) return next(`${specifier}.ts`, context)
      throw err
    }
  },
})

const { default: DiffMatchPatch } = await import('diff-match-patch')
const { createInstructionMarkdown } = await import('../../utils/instructionMarkdown.ts')
const {
  buildMarkdownDiff,
  diffVoteText,
  parseSentinels,
  piecesFromDiffOps,
  refinePieces,
} = await import('../../utils/markdownDiff.ts')

const md = createInstructionMarkdown({ voteText: diffVoteText })

// Readable form of a sentinel-marked string: [+ins] [-del] {-inline del}.
function show(text, src) {
  return parseSentinels(text).map((t) => {
    if (t.type === 'text') return t.value
    if (t.type === 'open') return t.mark === 'ins' ? '[+' : '[-'
    if (t.type === 'close') return ']'
    if (t.type === 'inline') return `{-${src.inlineMarks[t.index].text}}`
    return `<gap ${src.gaps[t.index]}>`
  }).join('')
}

function diff(before, after) {
  const dmp = new DiffMatchPatch()
  const ops = dmp.diff_main(before, after)
  dmp.diff_cleanupSemantic(ops)
  return piecesFromDiffOps(ops.map(([type, text]) => ({ type, text })))
}

function render(pieces) {
  const src = buildMarkdownDiff(pieces)
  const env = {}
  const html = md.render(src.source, env)
  return { src, html, shown: show(html, src), source: show(src.source, src), dir: env.docDir }
}

const BEFORE = `# כללי דיווח מכירות

KPI ראשי הוא **הכנסה נטו** לפי חודש.
https://wiki.example.com מכיל את ההגדרות המלאות.

## עמודות חשובות
- order_date משמש לסינון תאריכים
- **net_amount** אחרי הנחות ומע"מ
- customer_id מקשר ללקוח

| עמודה | משמעות |
|---|---|
| order_date | תאריך ההזמנה |
| net_amount | סכום נטו |

שאילתה לדוגמה:
\`\`\`sql
SELECT SUM(net_amount) FROM orders
\`\`\``

const AFTER = BEFORE
  .replace('# כללי דיווח מכירות', '# כללי דיווח מכירות ורווחיות')
  .replace('KPI ראשי הוא', 'Revenue הוא המדד הראשי:')
  .replace('- customer_id מקשר ללקוח', '- customer_id מקשר ללקוח\n- gross_margin הוא הרווח הגולמי')
  .replace('| net_amount | סכום נטו |', '| net_amount | סכום נטו |\n| gross_margin | רווח גולמי |')
  .replace('FROM orders', 'FROM sales_orders')

// --- the reproduced case: markdown renders, blocks read RTL --------------------
{
  const r = render(diff(BEFORE, AFTER))
  assert.equal(r.dir, 'rtl')
  // Heading, list, table, code: real elements, no raw syntax.
  assert.match(r.shown, /<h1 dir="rtl">כללי דיווח מכירות\[\+ ורווחיות\]<\/h1>/)
  assert.match(r.shown, /<h2 dir="rtl">עמודות חשובות<\/h2>/)
  assert.match(r.shown, /<li dir="rtl">\[\+gross_margin הוא הרווח הגולמי\]<\/li>/)
  assert.match(r.shown, /<table dir="rtl">/)
  assert.match(r.shown, /<td dir="rtl">\[\+ ?gross_margin ?\]<\/td>/)
  assert.match(r.shown, /<code class="language-sql">SELECT SUM\(net_amount\) FROM \{-orders\}\[\+sales_orders\]/)
  assert.doesNotMatch(r.shown, /\*\*|^#|\|---/m)
  // The paragraph that now opens with an English word is still RTL, and the
  // removed text sits in it — not in the heading above.
  assert.match(r.shown, /<p dir="rtl">\{-KPI ראשי הוא\}\[\+Revenue הוא המדד הראשי:\] <strong>הכנסה נטו<\/strong>/)
}

// --- deleted words do not vote ------------------------------------------------
{
  // A Hebrew line whose English opening was replaced: the struck English must
  // not tip a short line LTR.
  const r = render([
    { kind: 'delete', text: 'Total revenue KPI dashboard summary' },
    { kind: 'insert', text: 'סך ההכנסות' },
    { kind: 'equal', text: ' לפי חודש ורבעון' },
  ])
  assert.match(r.shown, /<p dir="rtl">/)
  // Whole-line deletions don't vote either.
  const w = render([
    { kind: 'delete', text: 'This entire English paragraph was removed from it\n' },
    { kind: 'equal', text: 'ההוראה בעברית נשארת כאן' },
  ])
  assert.equal(w.dir, 'rtl')
  const state = {}
  assert.deepEqual(diffVoteText('English', state), { vote: '', text: '' })
  assert.deepEqual(diffVoteText('more english', state), { vote: '', text: '' })
  assert.deepEqual(diffVoteText(' עברית', state), { vote: ' עברית', text: ' עברית' })
}

// --- an English-led Hebrew line is RTL (the dir="auto" bug) -------------------
{
  const r = render([{ kind: 'equal', text: 'order_date משמש לסינון תאריכים בכל הדוחות' }])
  assert.match(r.shown, /<p dir="rtl">/)
}

// --- removed inline syntax cannot change the structure of what remains --------
{
  // Bold removed: the rest is plain, the removed `**` is shown struck.
  const r = render(diff('המדד **הכנסה** נטו', 'המדד הכנסה נטו'))
  assert.doesNotMatch(r.html, /<strong>/)
  assert.match(r.shown, /\{-\*\*הכנסה\*\*\}\[\+הכנסה\]/)
  // A pipe removed from a table row does not add or drop a cell.
  const t = render(diff('| a | b |\n|---|---|\n| x | y |', '| a | b |\n|---|---|\n| x y |'))
  assert.match(t.html, /<table/)
}

// --- whole deleted lines keep their markdown ----------------------------------
{
  const r = render(diff('- אחד\n- שתיים\n- שלוש', '- אחד\n- שלוש'))
  assert.match(r.shown, /<li dir="rtl">\[-שתיים\]<\/li>/)
  assert.equal((r.html.match(/<li/g) || []).length, 3)
}

// --- block syntax stays outside the marks -------------------------------------
{
  const r = render([{ kind: 'equal', text: 'פסקה ראשונה\n\n' }, { kind: 'insert', text: '## כותרת חדשה\n- פריט חדש' }])
  assert.match(r.shown, /<h2 dir="rtl">\[\+כותרת חדשה\]<\/h2>/)
  assert.match(r.shown, /<li dir="rtl">\[\+פריט חדש\]<\/li>/)
}

// --- a change with nothing visible gets a ¶ so it can still be reviewed -------
{
  // Only a list marker was added: no text of its own.
  const src = buildMarkdownDiff([{ kind: 'equal', text: 'שורה\n' }, { kind: 'insert', text: '- ', hunk: 0 }, { kind: 'equal', text: 'פריט' }])
  assert.equal(src.inlineMarks.some(m => m.text === '¶' && m.hunk === 0 && m.mark === 'ins'), true)
  // A change with visible text gets no ¶.
  const plain = buildMarkdownDiff([{ kind: 'equal', text: 'א ' }, { kind: 'insert', text: 'ב', hunk: 0 }])
  assert.equal(plain.inlineMarks.length, 0)
}

// --- gaps are their own block -------------------------------------------------
{
  const r = render([
    { kind: 'equal', text: 'פסקה\n' },
    { kind: 'gap', lines: 7 },
    { kind: 'equal', text: '\nעוד פסקה' },
    { kind: 'insert', text: ' חדשה', hunk: 0 },
  ])
  assert.match(r.shown, /<p[^>]*><gap 7><\/p>/)
}

// --- hunks survive the line-aligned refinement --------------------------------
{
  const pieces = refinePieces([
    { kind: 'equal', text: 'כותרת' },
    { kind: 'delete', text: '\n\nKPI ראשי', hunk: 3 },
    { kind: 'insert', text: ' חדשה\n\nRevenue', hunk: 3 },
    { kind: 'equal', text: ' הוא המדד\nשורה אחרת' },
  ])
  const changes = pieces.filter(p => p.kind !== 'equal')
  assert.ok(changes.length > 0)
  assert.ok(changes.every(p => p.hunk === 3))
  // Text is preserved: equal + inserted = new text, equal + deleted = old text.
  const after = pieces.filter(p => p.kind !== 'delete').map(p => p.text).join('')
  const before = pieces.filter(p => p.kind !== 'insert').map(p => p.text).join('')
  assert.equal(after, 'כותרת חדשה\n\nRevenue הוא המדד\nשורה אחרת')
  assert.equal(before, 'כותרת\n\nKPI ראשי הוא המדד\nשורה אחרת')
}

// --- no change: renders exactly like InstructionText would -------------------
{
  const plainMd = createInstructionMarkdown()
  const text = '# כותרת\n\n- order_date משמש\n- פריט\n\n| a | b |\n|---|---|\n| 1 | 2 |'
  assert.equal(render([{ kind: 'equal', text }]).html, plainMd.render(text))
}

// --- a change inside a link target leaves no sentinel in the href -------------
{
  const r = render(diff('[דוח](https://a.example.com/x)', '[דוח](https://a.example.com/y)'))
  // markdown-it percent-encodes the sentinels; the browser renderer strips both forms.
  assert.match(r.html, /href="https:\/\/a\.example\.com\//)
}

// --- review regressions -------------------------------------------------------
{
  // Sentinel characters typed into the instruction cannot forge a mark or
  // claim another change's number (Accept would act on the wrong change).
  const forged = 'harmless  typo fix'
  const src = buildMarkdownDiff([{ kind: 'equal', text: 'א ' }, { kind: 'insert', text: forged, hunk: 1 }])
  const opens = parseSentinels(src.source).filter(t => t.type === 'open')
  assert.ok(opens.length > 0)
  assert.ok(opens.every(t => t.hunk === 1))
  assert.doesNotMatch(md.render(src.source), //)
}
{
  // A removed fence must not open a code block that swallows the kept text.
  const r = render(diff('x\n\n```\ncode line\n```\n\ny', 'x\n\ncode line\n\ny'))
  assert.doesNotMatch(r.html, /<pre>/)
  assert.match(r.shown, /<p[^>]*>y<\/p>/)
  // A removed setext underline must not turn the kept line into a heading.
  const h = render(diff('Intro\n---\nBody', 'Intro\nBody'))
  assert.doesNotMatch(h.html, /<h2/)
  assert.match(h.shown, /\{--{3}\}/)
}
{
  // A removed table (or list) with its blank line does not pull the next paragraph in.
  const t = render(diff('p\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nq', 'p\n\nq'))
  assert.match(t.shown, /<p[^>]*>q<\/p>/)
  // ...and the removed table still reads as a table, struck through.
  assert.match(t.html, /<table/)
  const l = render(diff('p\n\n- one\n- two\n\nq', 'p\n\nq'))
  assert.match(l.shown, /<p[^>]*>q<\/p>/)
}
{
  // Changes on lines that must stay plain structure are still shown and reviewable.
  const lang = buildMarkdownDiff([
    { kind: 'equal', text: '```' }, { kind: 'delete', text: 'sql', hunk: 0 }, { kind: 'insert', text: 'python', hunk: 0 },
    { kind: 'equal', text: '\nprint(1)\n```' },
  ])
  assert.ok(lang.inlineMarks.some(m => m.text.includes('sql') && m.mark === 'del' && m.hunk === 0))
  assert.ok(lang.inlineMarks.some(m => m.text.includes('python') && m.mark === 'ins' && m.hunk === 0))
  assert.equal(lang.invisibleHunks.size, 0)
  const rule = render([{ kind: 'equal', text: 'א\n\n' }, { kind: 'insert', text: '---\n\n', hunk: 0 }, { kind: 'equal', text: 'ב' }])
  assert.match(rule.html, /<hr>/)
  assert.match(rule.shown, /\{-?-{3}/)
  // The table stays a table when only its alignment row changes.
  const align = render(diff('| a | b |\n|---|---|\n| 1 | 2 |', '| a | b |\n|:--|---|\n| 1 | 2 |'))
  assert.match(align.html, /<table/)
  assert.equal(align.src.invisibleHunks.size, 0)
}
{
  // A changed @mention is replaced as a whole word, so it can still render as a chip.
  const pieces = refinePieces([{ kind: 'equal', text: 'Use ' }, { kind: 'delete', text: '@orders' }, { kind: 'insert', text: '@customers' }, { kind: 'equal', text: ' here' }])
  assert.deepEqual(pieces.filter(p => p.kind !== 'equal').map(p => p.text), ['@orders', '@customers'])
}

console.log('markdownDiff: ok')
