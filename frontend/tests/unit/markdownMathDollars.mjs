// Currency `$` must not be parsed as inline KaTeX math in chat replies.
//
// Run: cd frontend && node tests/unit/markdownMathDollars.mjs
import assert from 'node:assert/strict'
import { getMarkdown, parseMarkdownToStructure } from 'stream-markdown-parser'

import { escapeNonMathDollars, currencyDollarPlugin } from '../../utils/markdownMathDollars.ts'

// --- the string-level rule ---------------------------------------------------

// Two amounts in one sentence: neither `$` may pair up.
assert.equal(
  escapeNonMathDollars('Revenue was $2,329 and $5.65 per invoice.'),
  'Revenue was \\$2,329 and \\$5.65 per invoice.',
)
// Closing `$` followed by a digit is never a closer ("$5-$10").
assert.equal(escapeNonMathDollars('between $5-$10'), 'between \\$5-\\$10')
// Space after the opener or before the closer: not math.
assert.equal(escapeNonMathDollars('a $ b $ c'), 'a \\$ b \\$ c')
// Genuine inline math is left alone.
assert.equal(escapeNonMathDollars('where $x^2 + y^2 = r^2$ holds'), 'where $x^2 + y^2 = r^2$ holds')
assert.equal(escapeNonMathDollars('$a$ and $b$'), '$a$ and $b$')
// Currency before a formula: the amount is escaped, the formula is kept.
assert.equal(escapeNonMathDollars('costs $5 so $x=5$'), 'costs \\$5 so $x=5$')
// Display math, \( \) and already-escaped dollars are untouched.
assert.equal(escapeNonMathDollars('$$E = mc^2$$'), '$$E = mc^2$$')
assert.equal(escapeNonMathDollars('\\(a+b\\) costs \\$3'), '\\(a+b\\) costs \\$3')
// Code spans and inline HTML keep their dollars verbatim.
assert.equal(escapeNonMathDollars('run `echo $HOME` for $5'), 'run `echo $HOME` for \\$5')
assert.equal(escapeNonMathDollars('<span title="$1">x</span> $2 off'), '<span title="$1">x</span> \\$2 off')
// Idempotent: a second pass changes nothing.
const once = escapeNonMathDollars('total $2,329 across 412 invoices, avg $5.65')
assert.equal(escapeNonMathDollars(once), once)

// --- through the real markstream parser -------------------------------------

const md = getMarkdown('test', { apply: [currencyDollarPlugin] })
const nodesOf = (src) => parseMarkdownToStructure(src, md, { final: true })
const flat = (nodes) => nodes.flatMap((n) => [n, ...flat(n.children || [])])
const types = (src) => flat(nodesOf(src)).map((n) => n.type)
const text = (src) => flat(nodesOf(src)).filter((n) => n.type === 'text').map((n) => n.content).join('')

const reply =
  'The available dataset shows **$2,329 total revenue across** 412 invoices, ' +
  'with a weighted average invoice value of $5.65.'

// Without the plugin this is the bug: the span between the amounts is math.
const stock = getMarkdown('stock')
assert.ok(
  flat(parseMarkdownToStructure(reply, stock, { final: true })).some((n) => n.type === 'math_inline'),
  'expected the unpatched parser to reproduce the bug',
)

// With it: no math, both amounts visible verbatim, bold kept.
assert.ok(!types(reply).includes('math_inline'))
assert.ok(types(reply).includes('strong'))
assert.ok(text(reply).includes('$2,329 total revenue across'))
assert.ok(text(reply).includes('$5.65.'))
assert.ok(!text(reply).includes('\\'))

assert.ok(!types('Revenue was $2,329 and $5.65 per invoice.').includes('math_inline'))
assert.equal(text('Revenue was $2,329 and $5.65 per invoice.'), 'Revenue was $2,329 and $5.65 per invoice.')

// A genuine formula next to currency still renders as math.
const mixed = 'At $5.65 each, revenue is $R = p \\cdot q$ for the period.'
const mathNodes = flat(nodesOf(mixed)).filter((n) => n.type === 'math_inline')
assert.equal(mathNodes.length, 1)
assert.equal(mathNodes[0].content, 'R = p \\cdot q')
assert.ok(text(mixed).includes('At $5.65 each'))

// Block math and fenced code are not rewritten.
assert.ok(types('$$\nx^2\n$$').includes('math_block'))
const fenced = flat(nodesOf('```sh\necho $A and $B\n```')).find((n) => n.type === 'code_block')
assert.ok(fenced && fenced.code.includes('echo $A and $B'))

// Tables (inline content per cell) are covered too.
assert.ok(!types('| a | b |\n|---|---|\n| $1,200 | $3.40 |').includes('math_inline'))

console.log('currency dollars render as text; real inline math still renders')
