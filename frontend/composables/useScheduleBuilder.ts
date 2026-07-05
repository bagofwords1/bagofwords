/**
 * Shared recurring-schedule builder.
 *
 * Single source of truth for converting between the structured schedule inputs
 * used by the schedule UIs (interval + every-N + hour + weekdays + day-of-month)
 * and a 5-field cron string. Both the report-refresh CronModal and the
 * ScheduledPromptModal use these helpers so the two schedulers stay identical
 * and can't drift.
 *
 * Cron conventions produced/parsed here:
 *   minutes   -> `*​/N * * * *`
 *   hours     -> `0 *​/N * * *`
 *   weekdays  -> `0 H * * 1-5`
 *   week      -> `0 H * * d1,d2,...`   (0=Sun .. 6=Sat)
 *   month     -> `0 H DOM * *`
 *   day       -> `0 H * * *`
 */

export type RecurInterval = 'minutes' | 'hours' | 'day' | 'weekdays' | 'week' | 'month'

export interface RecurringSchedule {
  interval: RecurInterval
  everyN: number
  hour: number
  days: number[]
  dayOfMonth: number
}

export function defaultRecurringSchedule(): RecurringSchedule {
  return { interval: 'day', everyN: 15, hour: 8, days: [1], dayOfMonth: 1 }
}

/** Build a 5-field cron string from structured recurring inputs. */
export function buildRecurringCron(s: RecurringSchedule): string {
  if (s.interval === 'minutes') return `*/${s.everyN} * * * *`
  if (s.interval === 'hours') return `0 */${s.everyN} * * *`
  if (s.interval === 'weekdays') return `0 ${s.hour} * * 1-5`
  if (s.interval === 'week') {
    const days = [...s.days].sort((a, b) => a - b)
    return `0 ${s.hour} * * ${(days.length > 0 ? days : [1]).join(',')}`
  }
  if (s.interval === 'month') return `0 ${s.hour} ${s.dayOfMonth} * *`
  return `0 ${s.hour} * * *`
}

/**
 * Parse a 5-field cron string into structured recurring inputs. Returns a
 * partial patch (only the fields the cron determines); callers merge it over
 * their current state. Returns null for crons we can't map to the builder.
 */
export function parseRecurringCron(cron?: string): Partial<RecurringSchedule> | null {
  if (!cron) return null
  const parts = cron.trim().split(/\s+/)
  if (parts.length < 5) return null
  const [min, hour, dom, , dow] = parts

  if (min.startsWith('*/')) {
    return { interval: 'minutes', everyN: parseInt(min.slice(2)) || 15 }
  }
  if (hour.startsWith('*/')) {
    return { interval: 'hours', everyN: parseInt(hour.slice(2)) || 1 }
  }
  if (dow === '1-5') {
    return { interval: 'weekdays', hour: parseInt(hour) || 0 }
  }
  if (dom !== '*' && dow === '*') {
    return { interval: 'month', hour: parseInt(hour) || 0, dayOfMonth: parseInt(dom) || 1 }
  }
  if (dow !== '*') {
    // Comma list of days ("1,3,5" -> [1,3,5]). Keep 0 (Sunday); fall back to Mon.
    const parsedDays = dow.split(',')
      .map((d) => parseInt(d, 10))
      .filter((d) => !Number.isNaN(d) && d >= 0 && d <= 6)
    return {
      interval: 'week',
      hour: parseInt(hour) || 0,
      days: parsedDays.length > 0 ? [...new Set(parsedDays)].sort((a, b) => a - b) : [1],
    }
  }
  return { interval: 'day', hour: parseInt(hour) || 0 }
}
