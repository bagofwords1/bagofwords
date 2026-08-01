// Shared per-report live status for list badges (sidebar, /reports, project
// pages, home). Each list view reports the ids it renders via fetchActivity();
// results land in one shared map so every surface shows the same state for
// the same report. Backend: GET /api/reports/activity (see ReportService).
//
// Phase 1 freshness model: snapshot on navigation/fetch (no push channel yet).
export interface ReportActivity {
  id: string
  state: 'awaiting_user' | 'running' | 'queued' | 'idle'
  unread: boolean
  error: boolean
  last_activity_at?: string | null
}

export const useReportActivity = () => {
  const activityMap = useState<Record<string, ReportActivity>>('report-activity-map', () => ({}))

  // Snapshot the status of a set of visible reports. Merges into the shared
  // map (other surfaces may be tracking different ids).
  async function fetchActivity(ids: Array<string | undefined | null>) {
    const unique = [...new Set(ids.filter(Boolean) as string[])].slice(0, 100)
    if (!unique.length) return
    try {
      const { data, error } = await useMyFetch<{ activity: ReportActivity[] }>('/reports/activity', {
        method: 'GET',
        query: { ids: unique.join(',') },
      })
      if (!error.value && data.value?.activity) {
        const next = { ...activityMap.value }
        for (const a of data.value.activity) next[a.id] = a
        activityMap.value = next
      }
    } catch { /* non-fatal: badges just stay stale */ }
  }

  function activityFor(id: string): ReportActivity | undefined {
    return activityMap.value[id]
  }

  // Optimistically clear the unread flag (the report page also persists the
  // watermark via POST /reports/{id}/viewed).
  function markReadLocal(id: string) {
    const a = activityMap.value[id]
    if (a && (a.unread || a.error)) {
      activityMap.value = { ...activityMap.value, [id]: { ...a, unread: false, error: false } }
    }
  }

  // Persist the watermark + clear locally. Fire-and-forget from the report page.
  async function markViewed(id: string) {
    markReadLocal(id)
    try { await useMyFetch(`/reports/${id}/viewed`, { method: 'POST' }) } catch { /* non-fatal */ }
  }

  return { activityMap, fetchActivity, activityFor, markReadLocal, markViewed }
}
