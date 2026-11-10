import assert from 'node:assert/strict'

import { parseSlashCommand, matchingSlashCommands, parseCompactArgs } from '../../utils/slashCommands.ts'

// A composer command must be the whole message, so ordinary prompts that
// merely start with a slash or mention a command keep reaching the agent.

for (const text of ['/compact', '  /compact  ', '/COMPACT', '/compact focus on churn', '/compact keep 3', '/compact\nfocus on the Q3 numbers']) {
  assert.equal(parseSlashCommand(text)?.name, 'compact', text)
}
for (const text of ['/compactor', '/etc/hosts is broken', 'please /compact', 'compact', '/', '/unknown cmd', '']) {
  assert.equal(parseSlashCommand(text), null, text)
}
assert.equal(parseSlashCommand('/compact   focus on churn ').args, 'focus on churn')

// --- the menu: only while the command word is being typed --------------------

assert.deepEqual(matchingSlashCommands('/').map(c => c.name), ['compact'])
assert.deepEqual(matchingSlashCommands('/co').map(c => c.name), ['compact'])
assert.deepEqual(matchingSlashCommands('/compact').map(c => c.name), ['compact'])
assert.deepEqual(matchingSlashCommands('/compact focus'), [])
assert.deepEqual(matchingSlashCommands('/x'), [])
assert.deepEqual(matchingSlashCommands('hello /co'), [])

// --- /compact arguments -------------------------------------------------------

assert.deepEqual(parseCompactArgs(''), {})
for (const n of [1, 4, 20]) assert.deepEqual(parseCompactArgs(`keep ${n}`), { keep: n })
assert.deepEqual(parseCompactArgs('keep 99'), { keep: 20 })
assert.deepEqual(parseCompactArgs('keep 0'), { keep: 1 })
assert.deepEqual(parseCompactArgs('focus on the churn numbers'), { instructions: 'focus on the churn numbers' })
// "keep" inside a sentence is a focus, not a count.
assert.deepEqual(parseCompactArgs('keep the revenue numbers'), { instructions: 'keep the revenue numbers' })
assert.equal(parseCompactArgs('x'.repeat(5000)).instructions.length, 800)

console.log('slashCommands: ok')
