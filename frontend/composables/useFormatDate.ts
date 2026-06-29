// /composables/useFormatDate.ts
//
// Single source of truth for rendering stored UTC timestamps in the
// organization's configured timezone. Storage is always UTC; this is the
// display edge where we convert.
//
// The org timezone comes from OrganizationSettings.config.timezone (set in
// Settings → General). When it is unset we pass `timeZone: undefined` to
// Intl, which falls back to the viewer's browser timezone — i.e. the prior
// behaviour — so leaving the setting blank changes nothing.
//
// Locale follows the active i18n locale so dates read naturally in the org's
// language; callers can override per-call when they need a fixed format.

export const useFormatDate = () => {
  const { settings } = useOrgSettings()

  // Resolve the active locale defensively — some call sites (public share
  // pages) render outside an i18n context.
  let i18nLocale = { value: 'en' as string }
  try {
    i18nLocale = useI18n().locale as any
  } catch {
    // no i18n context; fall back to 'en'
  }

  const timeZone = computed<string | undefined>(
    () => (settings.value?.config?.timezone as string | undefined) || undefined,
  )

  // A bare "YYYY-MM-DDTHH:mm[:ss[.fff]]" (or space-separated) string with no
  // timezone designator. Some API responses still serialize naive UTC columns
  // (datetime.utcnow) without a 'Z', and `new Date()` would parse those as the
  // viewer's *local* time. Since storage is always UTC, we treat such strings
  // as UTC so the conversion below starts from the right base. Strings that
  // already carry a marker (Z or ±hh:mm) and date-only strings are left alone.
  const NAIVE_DATETIME = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/

  const toDate = (value: string | number | Date): Date => {
    if (value instanceof Date) return value
    if (typeof value === 'string' && NAIVE_DATETIME.test(value)) {
      return new Date(value.replace(' ', 'T') + 'Z')
    }
    return new Date(value)
  }

  /**
   * Low-level formatter: mirrors Intl.DateTimeFormat but injects the org
   * timeZone (and the i18n locale by default). Returns '' for empty input and
   * echoes the raw value for unparseable input.
   */
  const format = (
    value: string | number | Date | null | undefined,
    options: Intl.DateTimeFormatOptions = {},
    localeOverride?: string,
  ): string => {
    if (value === null || value === undefined || value === '') return ''
    const d = toDate(value)
    if (isNaN(d.getTime())) return typeof value === 'string' ? value : ''
    const loc = localeOverride || i18nLocale.value || 'en'
    return new Intl.DateTimeFormat(loc, { timeZone: timeZone.value, ...options }).format(d)
  }

  // Convenience presets (each still accepts overrides).
  const formatDate = (v: any, o: Intl.DateTimeFormatOptions = {}, loc?: string) =>
    format(v, { year: 'numeric', month: 'short', day: 'numeric', ...o }, loc)

  const formatDateTime = (v: any, o: Intl.DateTimeFormatOptions = {}, loc?: string) =>
    format(v, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', ...o }, loc)

  const formatTime = (v: any, o: Intl.DateTimeFormatOptions = {}, loc?: string) =>
    format(v, { hour: '2-digit', minute: '2-digit', ...o }, loc)

  return { format, formatDate, formatDateTime, formatTime, toDate, timeZone }
}
