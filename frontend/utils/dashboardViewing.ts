export interface ViewedVersion {
  version: number
  live?: boolean
}

// The page a conversation's Dashboard tab shows a version on, read-only
// (collab01 "viewing"). The live version is the published page -- what
// readers see, from the release's own copies -- so it still shows when the
// conversation it was made in has been deleted (its draft is gone with it).
// Any other version is drawn from its conversation's draft. `embed` drops the
// page's own bars (the tab has its own); a new browser tab gets the full page.
export function viewingPath(dashboardId: string, viewed: ViewedVersion, { embed = true } = {}): string {
  const id = encodeURIComponent(dashboardId)
  const qs = new URLSearchParams()
  if (!viewed.live) qs.set('version', String(viewed.version))
  if (embed) qs.set('embed', '1')
  const query = qs.toString()
  const path = viewed.live ? `/d/${id}` : `/d/${id}/draft`
  return query ? `${path}?${query}` : path
}
