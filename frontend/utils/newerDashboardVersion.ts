// Whether an open dashboard should offer a newer version than the one on
// screen. Both views that show a dashboard outside the run that edits it --
// the conversation's Dashboard pane (ArtifactFrame) and the draft page
// (SharedDashboardView) -- poll, then ask this. A version only counts as
// news once: what was already known at the last read is never offered again.

export interface NewerOffer {
  id: string
  number: number
  // Saved in another conversation: viewed read-only, not selected.
  elsewhere: boolean
}

// The draft page: `latest` is the same draft read minus ?version.
export function newerDraftVersion(
  shown: { version_id?: string | null; version_number?: number | null } | null | undefined,
  latest: { version_id?: string | null; version_number?: number | null } | null | undefined,
): NewerOffer | null {
  if (!shown?.version_id || !latest?.version_id) return null
  if (latest.version_id === shown.version_id) return null
  if (!(Number(latest.version_number) > Number(shown.version_number || 0))) return null
  return { id: String(latest.version_id), number: Number(latest.version_number), elsewhere: false }
}

interface HereVersion { id: string; version: number }
interface ElsewhereVersion { id: string; number: number; report_id?: string | null }

// The conversation pane. `newestHere` is the newest version of the open
// dashboard in this conversation; `knownHere` the ids the pane held before
// this read. `elsewhere` is the dashboard's full history; `knownElsewhere`
// the ids it held before (empty: first read, a baseline and not news).
export function newerPaneVersion(args: {
  selected: HereVersion | null | undefined
  newestHere: HereVersion | null | undefined
  knownHere: Set<string>
  elsewhere: ElsewhereVersion[]
  knownElsewhere: Set<string>
  reportId: string
}): NewerOffer | null {
  const { selected, newestHere, knownHere, elsewhere, knownElsewhere, reportId } = args
  if (!selected) return null
  let offer: NewerOffer | null = null
  if (newestHere && !knownHere.has(newestHere.id) && newestHere.version > selected.version) {
    offer = { id: newestHere.id, number: newestHere.version, elsewhere: false }
  }
  if (knownElsewhere.size) {
    const fresh = elsewhere
      .filter(v => !knownElsewhere.has(v.id) && String(v.report_id) !== String(reportId))
      .reduce<ElsewhereVersion | null>((m, v) => (!m || v.number > m.number ? v : m), null)
    if (fresh && fresh.number > Math.max(selected.version, offer?.number || 0)) {
      offer = { id: fresh.id, number: fresh.number, elsewhere: true }
    }
  }
  return offer
}
