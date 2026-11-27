import assert from 'node:assert/strict'

import { buildFixErrorPrompt, parseFixErrorPrompt, summarizeError } from '../../utils/fixErrorPrompt.ts'

// Round trip: what the pane sends, chat parses back into the card.
const req = { title: 'Sales "Q3"', artifactId: 'a1b2-c3', error: "TypeError: Cannot read properties of undefined (reading 'map')\n    at render (artifact.js:12)" }
assert.deepEqual(parseFixErrorPrompt(buildFixErrorPrompt(req)), req, 'build → parse round trip')

// Fix requests already in chat history: the exact text the pane sent before
// this change (copied verbatim from the old fixRenderError) still parses.
const legacy = 'The dashboard "Revenue" (artifact_id: 42) failed to render with this error:\n```\nReferenceError: x is not defined\n```\nPlease fix the artifact code so it renders correctly.'
assert.deepEqual(parseFixErrorPrompt(legacy), { title: 'Revenue', artifactId: '42', error: 'ReferenceError: x is not defined' }, 'legacy prompt parses')

// The prompt text itself must not drift: the agent sees exactly this.
assert.equal(buildFixErrorPrompt({ title: 'Revenue', artifactId: '42', error: 'ReferenceError: x is not defined' }), legacy, 'prompt text unchanged')

// An error that itself contains a fence still parses whole.
const fenced = { title: 'T', artifactId: '1', error: 'bad\n```\ninner\n```\nend' }
assert.deepEqual(parseFixErrorPrompt(buildFixErrorPrompt(fenced)), fenced, 'error containing ``` parses whole')

// CRLF and surrounding whitespace (as stored by some clients) still parse.
assert.ok(parseFixErrorPrompt('  ' + legacy.replace(/\n/g, '\r\n') + '\n'), 'CRLF + whitespace tolerated')

// Not a fix request: free text, a polish prompt, or a user who typed more.
assert.equal(parseFixErrorPrompt(null), null)
assert.equal(parseFixErrorPrompt(''), null)
assert.equal(parseFixErrorPrompt('fix the dashboard please'), null)
assert.equal(parseFixErrorPrompt('Polish the dashboard "T" (artifact_id: 1).\nTarget element:\n```html\n<div></div>\n```\nInstruction: bigger'), null)
assert.equal(parseFixErrorPrompt(legacy + '\nAlso make it blue.'), null, 'edited prompt falls back to plain text')

// Summary: first non-empty line, capped with an ellipsis.
assert.equal(summarizeError('\n  TypeError: boom  \n  at x'), 'TypeError: boom')
assert.equal(summarizeError('x'.repeat(200), 50).length, 50)
assert.ok(summarizeError('x'.repeat(200), 50).endsWith('…'))
assert.equal(summarizeError(''), '')
