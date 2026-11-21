import assert from 'node:assert/strict'

import { buildClarifyPrompt, parseClarifyPrompt } from '../../utils/clarifyPrompt.ts'

// The clarify form sends the agent "Q: … A: …" text; the chat bubble parses it
// back into a card. Build and parse must round-trip.
const pairs = [
  { question: 'Which period?', answers: ['Last 30 days'] },
  { question: 'Which regions?', answers: ['EMEA, APAC', 'Americas'] },
]
const prompt = buildClarifyPrompt(pairs)

assert.equal(
  prompt,
  'Q: Which period?  \nA: Last 30 days\n\nQ: Which regions?  \nA: \n- EMEA, APAC\n- Americas',
  'wire format the agent (and existing history) relies on is unchanged',
)
assert.deepEqual(parseClarifyPrompt(prompt), pairs, 'round-trips single and multi-pick answers')
assert.deepEqual(parseClarifyPrompt(prompt.replace(/\n/g, '\r\n')), pairs, 'tolerates CRLF')

// A free-text answer may span paragraphs; only a new "Q: " starts a new block.
assert.deepEqual(
  parseClarifyPrompt('Q: What did you mean?  \nA: first line\n\nsecond paragraph'),
  [{ question: 'What did you mean?', answers: ['first line\n\nsecond paragraph'] }],
  'multi-paragraph answer stays one answer',
)

// Ordinary messages keep rendering as prose.
assert.equal(parseClarifyPrompt('Q: is this a question? A: maybe'), null, 'inline Q/A text is not a clarify answer')
assert.equal(parseClarifyPrompt('Show revenue\n\nQ: by month?  \nA: yes'), null, 'text before the first block')
assert.equal(parseClarifyPrompt('Q: unanswered  \nA: '), null, 'empty answer')
assert.equal(parseClarifyPrompt(''), null)
assert.equal(parseClarifyPrompt(null), null)
