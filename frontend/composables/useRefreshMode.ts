/**
 * Refresh mode for a report: the single question "when does this report's data
 * rerun?", with three answers.
 *
 * The API stores the two settings independently — `cron_schedule` and
 * `refresh_on_view` are separate fields, and a caller that omits
 * `refresh_on_view` leaves it untouched. The exclusivity lives here, in the UI
 * layer, so the modal can ask one question instead of two. Keeping the mapping
 * in one pure place is what makes that constraint testable: a report that
 * carries both settings (set via the API, or by an older build of the modal)
 * has to resolve to exactly one mode, and saving a mode has to clear the other
 * field rather than silently leaving it on.
 */

export type RefreshMode = 'off' | 'recurring' | 'on_open'

export interface RefreshModeSettings {
  /** 'None' unschedules — the value the schedule endpoint expects. */
  cron_expression: string
  refresh_on_view: boolean
}

/**
 * Which mode a stored report is in.
 *
 * A recurring schedule wins when both are set: it is the more consequential
 * setting (it can email subscribers on every run), so it must not be the one
 * that disappears from the UI.
 */
export function refreshModeFromReport(report: { cron_schedule?: string | null; refresh_on_view?: boolean } | null | undefined): RefreshMode {
  if (report?.cron_schedule) return 'recurring'
  if (report?.refresh_on_view) return 'on_open'
  return 'off'
}

/**
 * The settings a mode saves. Both fields are always explicit: picking one mode
 * must turn the other off, which an omitted field would not do.
 */
export function refreshModeSettings(mode: RefreshMode, cron: string): RefreshModeSettings {
  return {
    cron_expression: mode === 'recurring' ? cron : 'None',
    refresh_on_view: mode === 'on_open',
  }
}
