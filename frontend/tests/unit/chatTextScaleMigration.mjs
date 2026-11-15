// A text size stored by an older menu must not pin a reader to that menu's
// default; only the larger steps, which were real choices, carry over.
//
// Run: cd frontend && node tests/unit/chatTextScaleMigration.mjs
import assert from 'node:assert/strict'

import { migrateLegacyTextScale, CHAT_TEXT_SCALES, DEFAULT_CHAT_TEXT_SCALE } from '../../composables/useChatDisplay.ts'

assert.ok(CHAT_TEXT_SCALES.includes(DEFAULT_CHAT_TEXT_SCALE), 'the default is one of the offered steps')
assert.ok(CHAT_TEXT_SCALES.includes(0.875), '14px is still offered, as "Small"')

// Old "Default" (14px) → no choice → the new default applies.
assert.equal(migrateLegacyTextScale('0.875'), null)
// Larger steps were real choices and carry over.
assert.equal(migrateLegacyTextScale('1'), 1)
assert.equal(migrateLegacyTextScale('1.125'), 1.125)
assert.equal(migrateLegacyTextScale('1.25'), 1.25)
// Nothing stored, or garbage: no choice.
assert.equal(migrateLegacyTextScale(null), null)
assert.equal(migrateLegacyTextScale('abc'), null)
assert.equal(migrateLegacyTextScale('0.5'), null)

console.log('chatTextScaleMigration: ok')
