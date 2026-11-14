// Direction rules for chat text (Hebrew/Arabic answers, user prompts).
//
// Run: cd frontend && node tests/unit/textDirection.mjs
import assert from 'node:assert/strict'

import {
  countDirWords,
  decideDir,
  decideMessageDir,
  dominantTextDir,
  firstStrongDir,
  isSignedNumber,
  markdownDirCounts,
  messageVoteCounts,
  resolveDirTree,
  wordDir,
} from '../../utils/textDirection.ts'

// --- word classification -----------------------------------------------------

assert.equal(wordDir('שלום'), 'rtl')
assert.equal(wordDir('Revenue'), 'ltr')
// "ו-region" is a Hebrew prefix glued to a Latin word: the first strong char wins.
assert.equal(wordDir('ו-region_code'), 'rtl')
// Quantities are neutral: their unit letters must not vote.
assert.equal(wordDir('1.2M'), null)
assert.equal(wordDir('$118'), null)
assert.equal(wordDir('-5%'), null)
assert.equal(wordDir('2024'), null)
assert.equal(wordDir('—'), null)
// Brackets and quotes are skipped: a parenthesised Hebrew word votes RTL, a
// parenthesised amount stays neutral.
assert.equal(wordDir('(אלטרנטיבי)'), 'rtl')
assert.equal(wordDir('“Genre'), 'ltr')
assert.equal(wordDir('(5%)'), null)
assert.equal(wordDir('($1.2M)'), null)

// --- plain strings (user prompts) -------------------------------------------

// A Hebrew question that opens with an English term or a mention is RTL —
// the browser's first-strong rule gets both wrong.
assert.equal(dominantTextDir('KPI dashboard: תבנה לי לוח עם ההכנסות לפי חודש'), 'rtl')
assert.equal(firstStrongDir('KPI dashboard: תבנה לי לוח'), 'ltr')
assert.equal(dominantTextDir('מה היו ההכנסות ברבעון האחרון לפי אזור?'), 'rtl')
assert.equal(dominantTextDir('Show revenue by region for Q3'), 'ltr')
// English sentence with one Hebrew word stays LTR.
assert.equal(dominantTextDir('The column סטטוס holds the current order state'), 'ltr')
// No letters at all: no opinion.
assert.equal(dominantTextDir('1,200 / 900 (2024)'), null)
assert.equal(dominantTextDir(''), null)

// --- blocks inside an answer -------------------------------------------------

const c = (text) => countDirWords(text)

// Short blocks follow the surrounding answer: an English heading in a Hebrew
// answer, and a Hebrew heading in an English one.
assert.equal(decideDir(c('Summary'), { context: 'rtl' }), 'rtl')
assert.equal(decideDir(c('Key Drivers'), { context: 'rtl' }), 'rtl')
assert.equal(decideDir(c('סיכום'), { context: 'ltr' }), 'ltr')
// Mid-stream, a block that is only "**MRR**" so far does not flash LTR.
assert.equal(decideDir(c('MRR'), { context: 'rtl' }), 'rtl')
// Without context a short block still votes on its own.
assert.equal(decideDir(c('Summary')), 'ltr')

// A long English paragraph inside a Hebrew answer keeps its own direction.
assert.equal(
  decideDir(c('This paragraph is a quoted English sentence from the source system'), { context: 'rtl' }),
  'ltr',
)
// A Hebrew paragraph that opens with a Latin token stays RTL.
assert.equal(decideDir(c('Revenue בחודש מרץ עלה ב-12% לעומת פברואר'), { context: 'ltr' }), 'rtl')

// Lists / tables in an RTL answer only turn LTR when almost entirely Latin.
const mixedList = 'North: 1.2M USD South: 0.9M USD אזור המרכז: 0.7M USD Online / E-commerce: 0.4M USD'
assert.equal(decideDir(c(mixedList), { context: 'rtl', container: true }), 'rtl')
assert.equal(decideDir(c(mixedList), { context: 'rtl' }), 'ltr') // a paragraph would not
const englishList = 'Open the report Click Share Choose the people to invite and send'
assert.equal(decideDir(c(englishList), { context: 'rtl', container: true }), 'ltr')
// In an LTR answer a list stays LTR unless almost entirely Hebrew.
assert.equal(decideDir(c(mixedList), { context: 'ltr', container: true }), 'ltr')
assert.equal(decideDir(c('Click Share בחר משתמשים'), { context: 'ltr', container: true }), 'ltr')
assert.equal(decideDir(c('חברת אלפא בע״מ בטא תעשיות גמא שירותים דלתא בע״מ'), { context: 'ltr', container: true }), 'rtl')
// A short list or table (a header row so far) keeps the surrounding direction.
assert.equal(decideDir(c('Region Customer Status Amount'), { context: 'rtl', container: true }), 'rtl')

// List items (minWords 3): a short Latin item stays with its Hebrew list, an
// English sentence bullet gets its own direction.
const item = (t) => decideDir(c(t), { context: 'rtl', container: true, minWords: 3 })
assert.equal(item('North: 1.2M USD'), 'rtl')
assert.equal(item('Online / E-commerce: 0.4M USD'), 'rtl')
assert.equal(item('Sub point: SQL query ran on'), 'ltr')
assert.equal(item('Revenue לפי חודש, עם קו השוואה לשנה שעברה'), 'rtl')

// No words to vote: inherit, or no opinion without a context.
assert.equal(decideDir(c('1,200'), { context: 'rtl' }), 'rtl')
assert.equal(decideDir(c('1,200')), null)

// Signed numbers need LTR layout inside RTL text.
for (const v of ['+12%', '-2%', '−5', '+1.2M', '-$5', '+ 14%']) assert.equal(isSignedNumber(v), true, v)
for (const v of ['12%', '1,200', '2024', 'ל-2.1%', '+972 3 1234567', 'abc', '']) assert.equal(isSignedNumber(v), false, v)

// --- resolveDirTree: one decision per document -------------------------------
// Shared by the chat observer, the instruction view and the instruction editor.

const node = (kind, text, children = []) => {
  const counts = countDirWords(text)
  for (const ch of children) { counts.rtl += ch.counts.rtl; counts.ltr += ch.counts.ltr }
  return { kind, counts, text, children, ref: text }
}
const sum = (nodes) => nodes.reduce((a, n) => ({ rtl: a.rtl + n.counts.rtl, ltr: a.ltr + n.counts.ltr }), { rtl: 0, ltr: 0 })

{
  // A Hebrew instruction: English heading, mixed list, English bullet, table.
  const heading = node('block', 'Summary')
  const intro = node('block', 'Revenue בחודש מרץ עלה ב-12% לעומת פברואר')
  const itemShort = node('item', 'North: 1.2M USD')
  const itemHe = node('item', 'אזור המרכז: 0.7M USD')
  const para = node('block', 'Sub point: SQL query ran on')
  const itemEn = node('item', '', [para])
  const list = node('list', '', [itemShort, itemHe, itemEn])
  const cellEn = node('cell', 'Region')
  const cellSigned = node('cell', '+14%')
  const cellHe = node('cell', 'חברת אלפא בע״מ')
  const row = node('row', '', [cellEn, cellSigned, cellHe])
  const table = node('table', '', [row])
  const roots = [heading, intro, list, table]
  assert.equal(resolveDirTree(roots, sum(roots)), 'rtl')
  assert.equal(heading.dir, 'rtl', 'short English heading follows the Hebrew document')
  assert.equal(intro.dir, 'rtl', 'Hebrew sentence opening with an English word')
  assert.equal(list.dir, 'rtl')
  assert.equal(itemShort.dir, 'rtl', 'short Latin item stays with its list')
  assert.equal(itemHe.dir, 'rtl')
  assert.equal(itemEn.dir, 'rtl', 'all markers stay on the list edge, including English items')
  assert.equal(para.dir, 'rtl', "an item's paragraph follows the list")
  assert.equal(table.dir, 'rtl')
  assert.equal(row.dir, 'rtl')
  assert.equal(cellEn.dir, 'rtl', 'cells follow their table')
  assert.equal(cellSigned.dir, 'ltr', 'signed number laid out LTR in an RTL table')
  assert.equal(cellSigned.signedNumber, true)
  assert.equal(cellHe.dir, 'rtl')
}
{
  // An English bullet with a Hebrew sub-list: the item votes on its own text.
  const sub = node('list', '', [node('item', 'תת-נקודה: לא לכלול הזמנות שבוטלו בחודש האחרון'), node('item', 'נקודה נוספת בעברית לגבי הדיווח')])
  const item = node('item', 'Always use the fiscal calendar from the finance team', [sub])
  const list = node('list', '', [item, node('item', 'נקודה ראשונה בעברית על הכנסות')])
  const roots = [node('block', 'הכנסות מחושבות תמיד לפני מע״מ ולאחר החזרות'), list]
  resolveDirTree(roots, sum(roots))
  assert.equal(item.dir, list.dir, 'an English item keeps its parent list marker edge')
  assert.equal(sub.dir, 'rtl', 'the Hebrew sub-list keeps its own direction')
}
{
  // An English document is untouched: Hebrew word in a sentence, short Hebrew item.
  const p = node('block', 'The orders table includes the column סטטוס for each order')
  const he = node('item', 'בחר משתמשים')
  const en = node('item', 'Click Share')
  const list = node('list', '', [en, he])
  const roots = [p, list]
  assert.equal(resolveDirTree(roots, sum(roots)), 'ltr')
  assert.deepEqual([p.dir, list.dir, en.dir, he.dir], ['ltr', 'ltr', 'ltr', 'ltr'])
}
{
  // Too short to vote: the hint wins; without one, the fallback (UI direction).
  const roots = [node('block', 'Revenue')]
  assert.equal(resolveDirTree(roots, sum(roots), { hint: 'rtl' }), 'rtl')
  assert.equal(roots[0].dir, 'rtl')
  assert.equal(resolveDirTree(roots, sum(roots), { fallback: 'rtl' }), 'rtl')
  assert.equal(resolveDirTree(roots, sum(roots)), 'ltr')
  // A hint is sticky, but an answer almost entirely in the other script wins.
  const en = [node('block', 'Here is the full translation of the paragraph into English as requested')]
  assert.equal(resolveDirTree(en, sum(en), { hint: 'rtl' }), 'ltr')
}

{
  // Settled content (an instruction): a short all-Hebrew table in an English
  // document is RTL — the 8-word streaming guard applies to live replies only.
  const cells = ['מדד', 'טבלה', 'נסיעות', 'חיובים'].map((t) => node('cell', t))
  const rows = [node('row', '', cells.slice(0, 2)), node('row', '', cells.slice(2))]
  const heTable = node('table', '', rows)
  const enTable = node('table', '', [node('row', '', [node('cell', 'Table'), node('cell', 'one row per trip')])])
  const intro = node('block', 'Filter the trips table on the void flag before you aggregate anything')
  const roots = [intro, enTable, heTable]
  resolveDirTree(roots, sum(roots), { settled: true })
  assert.equal(enTable.dir, 'ltr')
  assert.equal(heTable.dir, 'rtl', 'short all-Hebrew table in a settled English doc is RTL')
  assert.equal(cells[0].dir, 'rtl')
  // A live reply (not settled) keeps the guard: a short table follows the answer.
  resolveDirTree(roots, sum(roots))
  assert.equal(heTable.dir, 'ltr')
}

// --- one direction per chat message ------------------------------------------
// A reply split by tool calls renders as several markdown blocks. The message
// decides once, from its prose, and every block takes that direction.

{
  // Lines are bucketed by kind; code, URLs and link targets never vote.
  const md = [
    '## סיכום ההכנסות',
    'הנה הנתונים לפי `customer_segment` מתוך https://example.com/report',
    '',
    '- Acme Corp – North America',
    '1. Globex Inc',
    '',
    '| customer_name | region |',
    '|---|---|',
    '| Initech | EMEA |',
    '```sql',
    'SELECT customer_name, region FROM orders',
    '```',
    'ראו [the full dashboard](https://example.com/d/1)',
  ].join('\n')
  const c = markdownDirCounts(md)
  assert.deepEqual(c.prose, { rtl: 7, ltr: 3 }, 'prose: headings and sentences, no code or URLs')
  assert.equal(c.lists.ltr, 6)
  assert.equal(c.lists.rtl, 0)
  assert.equal(c.tables.rtl, 0)
  assert.ok(c.tables.ltr >= 4)
  // An unclosed fence (still streaming) hides the rest.
  assert.deepEqual(markdownDirCounts('```python\nprint("hello world")\nmore code here'), markdownDirCounts(''))
  // A table without leading pipes is still a table.
  assert.equal(markdownDirCounts('Name | Region | Revenue').tables.ltr, 3)
}

{
  // The vote uses prose when there is enough; lists and tables only fill in.
  const prose = 'הנה ההכנסות לפי לקוח ברבעון האחרון'
  const table = '| Customer | Region |\n|---|---|\n| Acme Corp | North America |\n| Globex Inc | EMEA West |'
  assert.deepEqual(messageVoteCounts([prose, table]), countDirWords(prose))
  // A message that is only a table votes with the table.
  assert.ok(messageVoteCounts([table]).ltr > 0)
}

const TABLE_EN = '| customer_name | region | revenue |\n|---|---|---|\n| Acme Corp | North America | 1200000 |\n| Globex Inc | EMEA West | 980000 |\n| Initech LLC | APAC South | 760000 |'
const LIST_EN = '- **Acme Corp** – Enterprise plan, North America\n- **Globex Inc** – Pro plan, EMEA\n- **Initech** – Starter plan, APAC'
const HE_INTRO = 'הרצתי את השאילתה על טבלת `orders` וקיבלתי את התוצאות הבאות, מסודרות לפי הכנסות'

for (const [label, rest] of [['table of English data', TABLE_EN], ['list of English names', LIST_EN]]) {
  // A Hebrew reply whose data block (split off by a tool call) is all Latin
  // stays RTL — with the question's direction, with a saved locale, or with
  // neither once the Hebrew prose has arrived.
  assert.equal(decideMessageDir({ texts: [HE_INTRO, rest], question: 'rtl' }), 'rtl', label)
  assert.equal(decideMessageDir({ texts: [HE_INTRO, rest], saved: 'rtl' }), 'rtl', label)
  assert.equal(decideMessageDir({ texts: [HE_INTRO, rest], fallback: 'ltr' }), 'rtl', label)
  // Even before the prose: the data alone does not override the prior.
  assert.equal(decideMessageDir({ texts: ['', rest], question: 'rtl' }), 'rtl', label)
}

// A saved language that disagrees with plainly Hebrew prose loses.
assert.equal(decideMessageDir({ texts: [HE_INTRO, TABLE_EN], saved: 'ltr', question: 'rtl' }), 'rtl')
assert.equal(decideMessageDir({ texts: [HE_INTRO], saved: 'ltr' }), 'rtl')
// ...but mixed prose under an explicit English answer stays English.
assert.equal(decideMessageDir({
  texts: ['Here is the breakdown of הכנסות by region for the last quarter, as you asked in English'],
  saved: 'ltr', question: 'rtl',
}), 'ltr')
// A Hebrew question answered entirely in English prose renders LTR.
assert.equal(decideMessageDir({
  texts: ['Here is the full translation of the paragraph into English as requested by you'],
  question: 'rtl',
}), 'ltr')
// Hebrew prose that embeds English identifiers keeps the RTL prior.
assert.equal(decideMessageDir({
  texts: ['ה-revenue של Acme Corp ב-Q3 עלה, בעיקר בזכות ה-enterprise plan ו-upsell'],
  question: 'rtl',
}), 'rtl')
// Mid-stream: the first few English words never flip a Hebrew prior.
assert.equal(decideMessageDir({ texts: ['**Revenue Summary** for Q3'], question: 'rtl' }), 'rtl')
// An English reply with a Hebrew customer name stays LTR.
assert.equal(decideMessageDir({
  texts: ['The top customer this quarter was חברת אלפא with the largest enterprise renewal of the year'],
  question: 'ltr',
}), 'ltr')
// No prior and too little text: the UI direction; no text at all: the prior.
assert.equal(decideMessageDir({ texts: ['Done'], fallback: 'rtl' }), 'rtl')
assert.equal(decideMessageDir({ texts: [], question: 'rtl', fallback: 'ltr' }), 'rtl')
assert.equal(decideMessageDir({ texts: [] }), 'ltr')

console.log('textDirection: ok')
