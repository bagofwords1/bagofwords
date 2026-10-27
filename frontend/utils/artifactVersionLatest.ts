export interface VersionRow {
  id: string
  artifact_id?: string | null
  mode: string
}

// Whether the selected version is the newest of its dashboard -- the editor's
// "Use this version" shows only when it is not. `list` is newest first.
//
// A version tied to its dashboard (artifact_id differs from its own id)
// compares with that dashboard's versions only: a conversation can hold
// several dashboards, and a newer one is not a newer version of this one.
// Versions from before that tie carry their own id as artifact_id (no lineage
// to go by), so they keep the earlier rule -- newest of their mode -- and the
// button stays on old history.
export function isNewestVersion(list: VersionRow[], selectedId: string | null | undefined): boolean {
  if (!selectedId || list.length === 0) return true
  const selected = list.find(a => a.id === selectedId)
  if (!selected) return true
  const tied = !!selected.artifact_id && selected.artifact_id !== selected.id
  const newest = tied
    ? list.find(a => a.artifact_id === selected.artifact_id)
    : list.find(a => a.mode === selected.mode && (!a.artifact_id || a.artifact_id === a.id))
  return !newest || newest.id === selected.id
}

// The newest version of one dashboard in `list` (newest first), or null when
// the conversation holds none of it. Edit from a dashboard's draft opens the
// editor on that dashboard, not on the conversation's newest version.
export function newestVersionOf(list: VersionRow[], dashboardId: string | null | undefined): string | null {
  if (!dashboardId) return null
  return list.find(a => a.artifact_id === dashboardId)?.id || null
}
