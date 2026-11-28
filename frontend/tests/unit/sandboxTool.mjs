import assert from 'node:assert/strict'

import {
  SANDBOX_CHIP_NOTES,
  sandboxNoteKind,
  sandboxResultNoteKinds,
} from '../../utils/sandboxTool.ts'

// Run: node tests/unit/sandboxTool.mjs

// A result with codes is read by its codes, in the order given.
assert.deepEqual(
  sandboxResultNoteKinds({ note_codes: ['restored_snapshot', 'files_restored', 'background_processes', 'deletion_scheduled'] }),
  ['restored', 'filesRestored', 'background', 'deletion'],
)
assert.deepEqual(sandboxResultNoteKinds({ note_codes: ['recreated'] }), ['fresh'])
assert.deepEqual(sandboxResultNoteKinds({ note_codes: ['created'] }), ['created'])
assert.deepEqual(sandboxResultNoteKinds({ note_codes: ['resumed', 'timeout', 'skipped_artifacts'] }), ['resumed', 'timeout', 'skipped'])

// The codes win over the wording of the notes, whatever the notes say.
assert.deepEqual(
  sandboxResultNoteKinds({ note_codes: ['resumed'], notes: ['The sandbox had to be recreated: its filesystem is fresh.'] }),
  ['resumed'],
)
// An empty list of codes means nothing to say, even next to an old-style sentence.
assert.deepEqual(sandboxResultNoteKinds({ note_codes: [], notes: ['A new sandbox was created for this report.'] }), [])
// A code this client does not know is left out; a repeated one is said once.
assert.deepEqual(sandboxResultNoteKinds({ note_codes: ['from_the_future', 'created', 'created'] }), ['created'])

// A result stored before the codes existed falls back to its sentences.
assert.deepEqual(
  sandboxResultNoteKinds({ notes: [
    'The sandbox had to be recreated and was restored from a snapshot taken at 2026-11-27 09:12 UTC: files and packages are as they were then.',
    'Still running in the sandbox after this run: sleep 900. They keep using its CPU and memory until they exit or the sandbox stops.',
    'Something this client has never heard of.',
  ] }),
  ['restored', 'background'],
)
assert.deepEqual(sandboxResultNoteKinds({}), [])
assert.deepEqual(sandboxResultNoteKinds(null), [])

// The sentence matcher itself is unchanged.
assert.equal(sandboxNoteKind('The sandbox was stopped and has been resumed with its files and packages.'), 'resumed')
assert.equal(sandboxNoteKind('Anything else'), 'other')

// What the status line and the skipped list already say gets no chip.
for (const kind of ['timeout', 'cancelled', 'skipped', 'other']) assert.ok(!SANDBOX_CHIP_NOTES.includes(kind))
assert.ok(SANDBOX_CHIP_NOTES.includes('filesRestored'))

console.log('sandboxTool: ok')
