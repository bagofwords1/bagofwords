// /composables/useRelativeTime.ts
//
// Single source of truth for "5m ago"-style timestamps. Companion to
// useFormatDate, which owns absolute times; this owns deltas.
//
// It existed in a dozen components as a hand-rolled copy, and the copies had
// drifted in three ways that all show as wrong times on screen:
//
//   * naive-UTC parsing. Most of the API still serializes datetime.utcnow()
//     without a 'Z', so `new Date(s)` reads it as the viewer's local time. A
//     run from an hour ago renders "just now" east of UTC and lands in the
//     future west of it. toDate() (from useFormatDate) is the fix, and half
//     the copies didn't have it.
//   * hardcoded English. Most copies returned `${m}m ago` literally, in an
//     app translated into ten languages.
//   * missing "just now". Copies without a sub-minute branch render "0m ago".
//
// Compact by design, and built on the existing translated catalog keys rather
// than Intl.RelativeTimeFormat: the catalogs already carry curated compact
// forms in all ten locales ("vor 5 Min.", "לפני 5 ד׳"), and Intl's own output
// is close enough that switching would discard that work to change wording in
// nine languages for no visible gain.

export const useRelativeTime = () => {
  const { formatDate, toDate } = useFormatDate()

  // Some call sites render outside an i18n context (public share pages).
  let translate: ((key: string, params?: any) => string) | null = null
  try {
    translate = useI18n().t as any
  } catch {
    translate = null
  }

  const FALLBACK: Record<string, string> = {
    'data.justNow': 'just now',
    'queries.timeMinutesAgo': '{n}m ago',
    'queries.timeHoursAgo': '{n}h ago',
    'queries.timeDaysAgo': '{n}d ago',
  }

  const t = (key: string, n?: number): string => {
    if (translate) return translate(key, { n })
    return (FALLBACK[key] || '').replace('{n}', String(n ?? ''))
  }

  /**
   * "just now" / "5m ago" / "3h ago" / "2d ago", switching to an absolute
   * date past a week — a delta stops being useful once it needs division.
   *
   * Deltas are clamped at zero: a timestamp a few seconds in the future
   * (clock skew between the API host and the browser) should read "just now",
   * never "in -3 minutes".
   */
  const relativeTime = (value?: string | number | Date | null): string => {
    if (value === null || value === undefined || value === '') return ''
    const d = toDate(value)
    if (isNaN(d.getTime())) return ''

    const seconds = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000))
    if (seconds < 60) return t('data.justNow')
    const minutes = Math.floor(seconds / 60)
    if (minutes < 60) return t('queries.timeMinutesAgo', minutes)
    const hours = Math.floor(minutes / 60)
    if (hours < 24) return t('queries.timeHoursAgo', hours)
    const days = Math.floor(hours / 24)
    if (days < 7) return t('queries.timeDaysAgo', days)
    // Past a week, an absolute date in the org's timezone reads better than
    // "43d ago" — and formatDate already localizes it.
    return formatDate(d)
  }

  return { relativeTime }
}
