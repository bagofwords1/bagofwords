import assert from 'node:assert/strict'

import { buildRecurringCron, parseRecurringCron } from '../../composables/useScheduleBuilder.ts'

// The dashboard schedule once offered whole hours only: the builder wrote a
// fixed `0` minute and the parser dropped whatever minute a saved cron had, so
// opening and re-saving an 08:30 schedule silently moved it to 08:00. The
// invariant: a fixed-time schedule round-trips its minute, and callers that
// never pass a minute keep producing `0 H ...`.

const base = { everyN: 15, hour: 8, days: [1], dayOfMonth: 1 }

const cases = [
  [{ ...base, interval: 'day', minute: 30 }, '30 8 * * *'],
  [{ ...base, interval: 'weekdays', hour: 14, minute: 45 }, '45 14 * * 1-5'],
  [{ ...base, interval: 'week', hour: 0, minute: 5, days: [5, 0, 3] }, '5 0 * * 0,3,5'],
  [{ ...base, interval: 'month', hour: 23, minute: 59, dayOfMonth: 15 }, '59 23 15 * *'],
]

for (const [schedule, cron] of cases) {
  assert.equal(buildRecurringCron(schedule), cron)
  const parsed = parseRecurringCron(cron)
  assert.equal(parsed.interval, schedule.interval, cron)
  assert.equal(parsed.hour, schedule.hour, cron)
  assert.equal(parsed.minute, schedule.minute, cron)
  assert.equal(buildRecurringCron({ ...base, ...parsed }), cron, `round-trip ${cron}`)
}

// No minute given (the automations modal) -> whole hour, exactly as before.
assert.equal(buildRecurringCron({ ...base, interval: 'day' }), '0 8 * * *')
assert.equal(buildRecurringCron({ ...base, interval: 'week', days: [1, 3] }), '0 8 * * 1,3')

// Interval schedules carry no fixed minute and are unchanged.
assert.equal(buildRecurringCron({ ...base, interval: 'minutes', minute: 30 }), '*/15 * * * *')
assert.equal(buildRecurringCron({ ...base, interval: 'hours', everyN: 2, minute: 30 }), '0 */2 * * *')
assert.equal(parseRecurringCron('*/15 * * * *').minute, undefined)
assert.equal(parseRecurringCron('0 */2 * * *').minute, undefined)

console.log('scheduleBuilder: ok')
