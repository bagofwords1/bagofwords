/**
 * How the quota editor turns its text inputs back into API values.
 *
 * The inputs are rounded for people: spend to cents, data to 0.01 MB. The API
 * keeps full precision (a $0.004 daily spend cap, a 1,024-byte data cap). So a
 * value the admin didn't touch must go back exactly as it came, or a name-only
 * edit would silently re-save "$0.004" as 0 and "1,024 B" as 0 MB, which blocks
 * all usage. Only a field whose text changed is parsed again.
 */

export type LimitBaseline = { input: string; raw: number | null }

export function limitValue(
  input: string | number | null | undefined,
  baseline: LimitBaseline | undefined,
  parse: (value: string | number | null | undefined) => number | null,
): number | null {
  if (baseline && String(input ?? '').trim() === baseline.input.trim()) return baseline.raw
  return parse(input)
}

/**
 * Daily and monthly caps are compared only where they cover the same scope.
 * Tokens and spend are per user on both windows. The monthly query and data
 * caps apply per connection while the daily ones count every connection
 * together, so a daily query cap above the monthly one is a valid policy.
 */
export const COMPARABLE_DAILY_MONTHLY = ['tokens', 'spend'] as const

export function dailyExceedsMonthly(metricKey: string, daily: string, monthly: string): boolean {
  if (!(COMPARABLE_DAILY_MONTHLY as readonly string[]).includes(metricKey)) return false
  const d = String(daily ?? '').trim()
  const m = String(monthly ?? '').trim()
  return d !== '' && m !== '' && Number(d) > Number(m)
}
