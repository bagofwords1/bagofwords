import assert from 'node:assert/strict'

import {
  citationKeyFromHref,
  citeText,
  collectSources,
  matchSource,
  stripCitations,
} from '../../composables/useCitations.ts'
import { describeDataOutput } from '../../composables/useRunActivity.ts'

// Run: node tests/unit/useCitations.mjs

const uuid = (n) => `${String(n).repeat(8)}-aaaa-bbbb-cccc-${String(n).repeat(12)}`
let seq = 0
function result(vizId, rows, extra = {}) {
  seq += 1
  return {
    id: `b${seq}`,
    status: 'completed',
    tool_execution: {
      tool_name: 'create_data',
      status: 'success',
      arguments_json: { title: `Result ${seq}` },
      created_step: { data: { rows } },
      result_json: { created_visualization_ids: [vizId], data_model: { type: 'table' } },
      ...extra,
    },
  }
}
const rows = [{ a: 1, b: 2 }, { a: 3, b: 4 }]
const blocks = [
  { id: 'p1', content: 'Checking.' },
  { id: 'i1', tool_execution: { tool_name: 'inspect_data', status: 'success' } },
  result(uuid(1), rows),
  result(uuid(2), [{ total: 7 }]),
  result(uuid(3), rows, { status: 'error' }),
]
const sources = collectSources(blocks, describeDataOutput)
const [s1, s2] = sources

// --- sources -----------------------------------------------------------------

// Only successful create_data results can be cited: not inspections, not failures.
assert.deepEqual(sources.map((s) => s.key), [blocks[2].id, blocks[3].id])
assert.deepEqual(sources.map((s) => s.output.kind), ['table', 'number'])

// A source is named by its full id or its leading characters, any spelling.
assert.equal(matchSource(uuid(1), sources), s1)
assert.equal(matchSource(uuid(1).slice(0, 8), sources), s1)
assert.equal(matchSource(uuid(2).toUpperCase(), sources), s2)
// Too short to identify anything; an id of nothing in the conversation.
assert.equal(matchSource(uuid(1).slice(0, 5), sources), null)
assert.equal(matchSource(uuid(9), sources), null)
// Two results share the cited prefix: no guess between them.
{
  const twins = collectSources([result(uuid(4), rows), result(`${'4'.repeat(8)}-ffff-bbbb-cccc-${'5'.repeat(12)}`, rows)], describeDataOutput)
  assert.equal(matchSource('44444444', twins), null)
  assert.equal(matchSource(uuid(4), twins), twins[0])
}

// --- rendering ---------------------------------------------------------------

const cite = (id) => `{{cite:${id}}}`

// Markers become chip links numbered by first mention; a repeat keeps its number.
{
  const text = `B first. ${cite(uuid(2).slice(0, 8))}\nThen A. ${cite(uuid(1))}\nB again.{{ cite : ${uuid(2)} }}`
  const { markdown, cited } = citeText(text, sources)
  assert.deepEqual(cited, [s2, s1])
  assert.ok(!markdown.includes('{{'))
  const links = [...markdown.matchAll(/\[(\d+)\]\((\S+)(?: "([^"]*)")?\)/g)].map((m) => [m[1], citationKeyFromHref(m[2]), m[3]])
  // Each chip links to its source and carries the source's name for its label.
  assert.deepEqual(links, [
    ['1', s2.key, s2.output.title], ['2', s1.key, s1.output.title], ['1', s2.key, s2.output.title],
  ])
  // The sentence itself is untouched.
  assert.equal(markdown.replace(/\u00A0\[\d+\]\([^)]+\)/g, ''), 'B first.\nThen A.\nB again.')
}

// Numbering is shared across the blocks of one message when the caller threads it.
{
  const numbering = []
  citeText(`One. ${cite(uuid(1))}`, sources, numbering)
  const second = citeText(`Two. ${cite(uuid(2))} One again. ${cite(uuid(1))}`, sources, numbering)
  assert.deepEqual(numbering, [s1, s2])
  assert.match(second.markdown, /Two\. \[2\]/)
  assert.match(second.markdown, /again\. \[1\]/)
}

// A citation of nothing in the conversation is removed, not shown.
{
  const { markdown, cited } = citeText(`A claim. ${cite(uuid(9))} Next.`, sources)
  assert.equal(markdown, 'A claim. Next.')
  assert.deepEqual(cited, [])
}

// Text with no markers, and non-citation braces, pass through untouched.
for (const text of ['', 'Plain answer.', 'A {{viz:abc}} embed and a {single} brace.']) {
  assert.equal(citeText(text, sources).markdown, text)
  assert.equal(stripCitations(text), text)
}

// A code fence is quoted text.
{
  const fence = '```\nappend ' + cite(uuid(1)) + ' after a sentence\n```'
  assert.equal(citeText(`Prose. ${cite(uuid(1))}\n${fence}`, sources).markdown.endsWith(fence), true)
  assert.equal(stripCitations(fence), fence)
}

// While streaming, a marker that has not finished arriving is hidden at every
// cut point; once the text is final nothing is hidden.
{
  const full = `Revenue grew. ${cite(uuid(1).slice(0, 8))}`
  const prefix = 'Revenue grew.'
  for (let i = prefix.length; i < full.length; i++) {
    const shown = citeText(full.slice(0, i), sources, [], { streaming: true }).markdown
    assert.equal(shown.trimEnd(), prefix, `cut at ${i}: ${JSON.stringify(shown)}`)
  }
  assert.match(citeText(full, sources, [], { streaming: true }).markdown, /\[1\]/)
  assert.equal(citeText('An open brace {', sources).markdown, 'An open brace {')
}

// Plain-text uses (clipboard) drop every marker and keep the prose.
assert.equal(stripCitations(`A. ${cite(uuid(1))} B.${cite(uuid(9))}`), 'A. B.')

// A title that would break the link syntax is neutralised, not dropped.
{
  const odd = collectSources([result(uuid(6), rows)], describeDataOutput)
  odd[0].output.title = 'Says "hi"\nthere'
  const { markdown } = citeText(`X. ${cite(uuid(6))}`, odd)
  assert.ok(markdown.endsWith('"Says  hi  there")'), markdown)
}

// Only chip links carry a source key.
assert.equal(citationKeyFromHref('https://example.com/x'), '')
assert.equal(citationKeyFromHref(`#cite-${s1.key}`), s1.key)
// An ordinary link that merely contains `#cite-` is not a chip (ChatLink uses startsWith too).
assert.equal(citationKeyFromHref(`http://localhost:3000/reports/1#cite-${s1.key}`), '')
assert.equal(citationKeyFromHref('https://docs.example.com/page#cite-ref-2'), '')
assert.equal(citationKeyFromHref('#cite-%E0%A4%A'), '%E0%A4%A')

// A saved query materialized by describe_entity (create mode) is a source like
// a create_data result: the answer can cite it, and the sources row lists it.
// A describe_entity lookup that created nothing is not a source.
{
  const materialized = {
    id: 'b-entity', status: 'completed',
    tool_execution: {
      tool_name: 'describe_entity', status: 'success',
      arguments_json: { name_or_id: 'Tracks wide', should_create: true },
      created_step_id: 's-tracks',
      result_json: { title: 'Tracks wide', data: { rows }, created_visualization_ids: [uuid(7)] },
    },
  }
  const lookup = {
    id: 'b-lookup', status: 'completed',
    tool_execution: {
      tool_name: 'describe_entity', status: 'success',
      arguments_json: { name_or_id: 'Tracks wide' },
      result_json: { title: 'Tracks wide', data: { rows }, created_visualization_ids: [uuid(8)] },
    },
  }
  const found = collectSources([lookup, materialized], describeDataOutput)
  assert.deepEqual(found.map((s) => [s.key, s.output.kind, s.output.title]), [['b-entity', 'table', 'Tracks wide']])
  const { markdown, cited } = citeText(`The tracks run 3 to 4.5 minutes. ${cite(uuid(7).slice(0, 8))}`, found)
  assert.deepEqual(cited.map((s) => s.key), ['b-entity'])
  assert.match(markdown, /\[1\]\(#cite-b-entity/)
}

// --- figures: {{show:<id>}} --------------------------------------------------

const show = (id) => `{{show:${id}}}`
// A figure is a <bow-figure> block numbered with the citations: figure 1 is
// source 1, and a later chip for the same result reuses its number.
{
  const text = `Revenue by store. ${cite(uuid(1).slice(0, 8))}\n\n${show(uuid(1).slice(0, 8))}\n\nAnd again. ${cite(uuid(1))}`
  const { markdown, cited } = citeText(text, sources, [], { figures: true })
  assert.deepEqual(cited, [s1])
  assert.match(markdown, new RegExp(`\n\n<bow-figure source="${s1.key}" number="1"></bow-figure>\n\n`))
  assert.equal((markdown.match(/\[1\]\(#cite-/g) || []).length, 2, 'chips before and after keep number 1')
  assert.doesNotMatch(markdown, /\n{3,}/)
}
// A figure first takes number 1; the chip that follows is 2.
{
  const shown = collectSources([result(uuid(5), rows), result(uuid(6), rows)], describeDataOutput)
  const { markdown } = citeText(`${show(uuid(5))}\n\nThen. ${cite(uuid(6))}`, shown, [], { figures: true })
  assert.match(markdown, /number="1"/)
  assert.match(markdown, /\[2\]\(#cite-/)
}
// Named mid-sentence, in backticks or parentheses: the sentence is tidied and
// the figure lands after its paragraph.
{
  const text = `The chart is above (\`${show(uuid(1).slice(0, 8))}\`).\n\nNext paragraph.`
  const { markdown } = citeText(text, sources, [], { figures: true })
  assert.equal(markdown, `The chart is above.\n\n<bow-figure source="${s1.key}" number="1"></bow-figure>\n\nNext paragraph.`)
  const inline = citeText(`See ${show(uuid(1))} for the split. ${cite(uuid(2))}`, sources, [], { figures: true }).markdown
  assert.match(inline, /^See for the split\.\u00A0\[2\]\(#cite-[^)]*\)\n\n<bow-figure source="[^"]+" number="1"><\/bow-figure>$/)
}
// highlight="…" travels to the figure as text (escaped, capped); other attributes are dropped.
{
  const { markdown } = citeText(`A.\n\n{{show:${uuid(1).slice(0, 8)} highlight="USA, Canada" color="red"}}`, sources, [], { figures: true })
  assert.match(markdown, new RegExp(`<bow-figure source="${s1.key}" number="1" highlight="USA, Canada"></bow-figure>`))
  assert.doesNotMatch(markdown, /color/)
  const evil = citeText(`{{show:${uuid(1)} highlight="<img src=x>"}}`, sources, [], { figures: true }).markdown
  assert.match(evil, /highlight="&lt;img src=x&gt;"/)
}
// Never a figure: a single value, a made-up id, or a surface without figures.
{
  const one = citeText(`Total. ${show(uuid(2))}`, sources, [], { figures: true })
  assert.doesNotMatch(one.markdown, /bow-figure|show/)
  assert.deepEqual(one.cited, [])
  assert.doesNotMatch(citeText(show(uuid(9)), sources, [], { figures: true }).markdown, /bow-figure|show/)
  const off = citeText(`A.\n\n${show(uuid(1))}\n\nB.`, sources)
  assert.equal(off.markdown, 'A.\n\nB.')
}
// Streaming: a half-received figure marker never flashes.
for (const partial of ['{{s', '{{sho', '{{show:', '{{show:1111', '{{show:11111111}']) {
  assert.equal(citeText(`Done.\n\n${partial}`, sources, [], { figures: true, streaming: true }).markdown, 'Done.\n\n')
}
// Inside a code fence it is quoted text; stripCitations removes it elsewhere.
{
  const fenced = '```\n{{show:11111111}}\n```'
  assert.equal(citeText(fenced, sources, [], { figures: true }).markdown, fenced)
  assert.equal(stripCitations(`A.\n\n${show(uuid(1))}\n\nB.`), 'A.\n\nB.')
}

console.log('useCitations: all assertions passed')
