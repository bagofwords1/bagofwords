// The text-size default moved from 14px to 15px. A size stored by the old
// menu must not pin readers to 14px: there, 14px was the "Default" step.
//
// Run: cd frontend && node tests/unit/chatTextScaleMigration.mjs
import assert from 'node:assert/strict'

import { migrateLegacyTextScale, CHAT_TEXT_SCALES, DEFAULT_CHAT_TEXT_SCALE } from '../../composables/useChatDisplay.ts'

assert.equal(DEFAULT_CHAT_TEXT_SCALE, 0.9375, 'default is 15px')
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
