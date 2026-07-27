import assert from 'node:assert/strict'

import { refreshModeFromReport, refreshModeSettings } from '../../composables/useRefreshMode.ts'

// --- reading a stored report -------------------------------------------------

assert.equal(refreshModeFromReport({ cron_schedule: null, refresh_on_view: false }), 'off')
assert.equal(refreshModeFromReport({ cron_schedule: '0 8 * * *', refresh_on_view: false }), 'recurring')
assert.equal(refreshModeFromReport({ cron_schedule: null, refresh_on_view: true }), 'on_open')

// A report can carry both — the fields are independent server-side, so the API
// and older builds of the modal can produce this. The schedule wins, because it
// is the setting that can email subscribers; hiding it would be the worse
// surprise.
assert.equal(
  refreshModeFromReport({ cron_schedule: '0 8 * * *', refresh_on_view: true }),
  'recurring',
  'a report with both settings must display as recurring',
)

assert.equal(refreshModeFromReport(null), 'off')
assert.equal(refreshModeFromReport({}), 'off')

console.log('stored reports resolve to exactly one mode')

// --- saving a mode -----------------------------------------------------------

// The exclusivity that the three-way control promises lives only in the UI: the
// API leaves an omitted refresh_on_view untouched, so each mode has to send
// both fields and actively turn the other one off.
assert.deepEqual(
  refreshModeSettings('recurring', '0 8 * * *'),
  { cron_expression: '0 8 * * *', refresh_on_view: false },
  'picking recurring must clear refresh-on-view',
)

assert.deepEqual(
  refreshModeSettings('on_open', '0 8 * * *'),
  { cron_expression: 'None', refresh_on_view: true },
  'picking on-open must unschedule the cron, not keep it running alongside',
)

assert.deepEqual(
  refreshModeSettings('off', '0 8 * * *'),
  { cron_expression: 'None', refresh_on_view: false },
  'off must clear both',
)

console.log('each mode clears the other setting')

// --- round trip --------------------------------------------------------------

for (const mode of ['off', 'recurring', 'on_open']) {
  const saved = refreshModeSettings(mode, '0 8 * * *')
  const reread = refreshModeFromReport({
    cron_schedule: saved.cron_expression === 'None' ? null : saved.cron_expression,
    refresh_on_view: saved.refresh_on_view,
  })
  assert.equal(reread, mode, `mode ${mode} did not survive a save/reload round trip`)
}

console.log('every mode survives a save and reopen')
