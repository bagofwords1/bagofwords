import assert from 'node:assert/strict'

import { parseReportScope, snippetParts } from '../../utils/commandPaletteScope.ts'

// --- scope prefix ---------------------------------------------------------
const prefixes = ['report:', 'דוח:']

for (const q of ['report: churn', 'REPORT:churn', '  Report:   churn  ']) {
  assert.deepEqual(parseReportScope(q, prefixes), { scoped: true, term: 'churn' }, q)
}
// A locale alias works next to the English one (RTL included).
assert.deepEqual(parseReportScope('דוח: נטישה', prefixes), { scoped: true, term: 'נטישה' })
// The prefix alone is a scoped search with nothing typed yet.
assert.deepEqual(parseReportScope('report:', prefixes), { scoped: true, term: '' })
// Not at the start → plain text, searched everywhere.
assert.deepEqual(parseReportScope('churn report: q3', prefixes), { scoped: false, term: 'churn report: q3' })
assert.deepEqual(parseReportScope('reports', prefixes), { scoped: false, term: 'reports' })
// Empty / missing alias entries are ignored rather than matching everything.
assert.deepEqual(parseReportScope('churn', ['report:', '', '  ']), { scoped: false, term: 'churn' })

// --- snippet windowing ----------------------------------------------------
const join = (parts) => parts.map(p => p.text).join('')
const hits = (parts) => parts.filter(p => p.hit).map(p => p.text)

// Short snippet: unchanged, the match (any case) is the emphasized part.
let parts = snippetParts('Why did Churn rise in March?', 'churn')
assert.equal(join(parts), 'Why did Churn rise in March?')
assert.deepEqual(hits(parts), ['Churn'])

// Match far into the snippet: the front is trimmed (on a word boundary) so the
// match stays near the start of a one-line, truncated row.
const long = 'Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor retention dropped'
parts = snippetParts(long, 'retention')
assert.ok(join(parts).startsWith('…'))
assert.ok(join(parts).indexOf('retention') <= 35, join(parts))
assert.ok(long.endsWith(join(parts).slice(1)), 'trimmed text is a suffix of the snippet')
assert.deepEqual(hits(parts), ['retention'])

// No match (e.g. the server matched across collapsed whitespace): plain text.
parts = snippetParts('nothing here', 'zebra')
assert.deepEqual(parts, [{ text: 'nothing here', hit: false }])
assert.deepEqual(snippetParts('anything', ''), [{ text: 'anything', hit: false }])
