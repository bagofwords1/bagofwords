// Direction rules for chat text (Hebrew/Arabic answers, user prompts).
//
// Run: cd frontend && node tests/unit/textDirection.mjs
import assert from 'node:assert/strict'

import {
  countDirWords,
  decideDir,
  dominantTextDir,
  firstStrongDir,
  isSignedNumber,
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

console.log('textDirection: ok')
