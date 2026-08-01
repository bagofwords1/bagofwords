// Shared per-report live status for list badges (sidebar, /reports, project
// pages, home). Each list view reports the ids it renders via fetchActivity();
// results land in one shared map so every surface shows the same state for
// the same report. Backend: GET /api/reports/activity (snapshot) and
// GET /api/reports/activity/stream (SSE deltas from the per-worker watcher).
//
// Freshness model: snapshot on fetch/(re)connect, live deltas from the
// stream while the tab is visible. A dropped event is corrected by the next
// snapshot — deltas are best-effort, snapshots are truth.
export interface ReportActivity {
  id: string
  state: 'awaiting_user' | 'running' | 'queued' | 'idle'
  unread: boolean
  error: boolean
  last_activity_at?: string | null
}

// Module-level singletons: one stream + one tracked-id set per tab.
const trackedIds = new Set<string>()
let streamController: AbortController | null = null
let streamGeneration = 0
let visibilityHooked = false

export const useReportActivity = () => {
  const activityMap = useState<Record<string, ReportActivity>>('report-activity-map', () => ({}))
  // The report the user is currently reading: events for it never flag unread.
  const activeReportId = useState<string | null>('report-activity-active', () => null)

  // Snapshot the status of a set of visible reports. Merges into the shared
  // map (other surfaces may be tracking different ids).
  async function fetchActivity(ids: Array<string | undefined | null>) {
    const unique = [...new Set(ids.filter(Boolean) as string[])].slice(0, 100)
    if (!unique.length) return
    unique.forEach(id => trackedIds.add(id))
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

  // Stable sort helper for list surfaces: starred first (when the row model
  // carries is_starred), then latest activity — live value when the stream
  // has newer truth than the fetched row. Reads activityMap so computeds
  // using it re-sort when events arrive.
  function sortByActivity<T extends { id: string; is_starred?: boolean; last_activity_at?: string | null; created_at?: string }>(reports: T[]): T[] {
    const ts = (r: T) => {
      const live = activityMap.value[r.id]?.last_activity_at
      const base = r.last_activity_at || r.created_at || ''
      return (live && live > base ? live : base) || ''
    }
    return [...reports].sort((a, b) => {
      const star = Number(!!b.is_starred) - Number(!!a.is_starred)
      if (star !== 0) return star
      return ts(b).localeCompare(ts(a))
    })
  }

  // ── live deltas ──────────────────────────────────────────────────────────
  function applyEvent(ev: { report_id: string; state: ReportActivity['state']; error: boolean; last_activity_at?: string | null }) {
    const prev = activityMap.value[ev.report_id]
    // New reports (e.g. spawned by a schedule) surface on the next snapshot;
    // without a row model there is nothing to badge yet.
    if (!prev && !trackedIds.has(ev.report_id)) return
    const activityMoved = !!ev.last_activity_at && ev.last_activity_at !== (prev?.last_activity_at || null)
    const isActive = activeReportId.value === ev.report_id
    const unread = isActive ? false : (activityMoved ? true : (prev?.unread ?? false))
    activityMap.value = {
      ...activityMap.value,
      [ev.report_id]: {
        id: ev.report_id,
        state: ev.state,
        error: ev.error,
        last_activity_at: ev.last_activity_at ?? prev?.last_activity_at ?? null,
        unread,
      },
    }
  }

  async function openStream() {
    if (typeof window === 'undefined') return
    if (streamController) return // singleton per tab
    const gen = ++streamGeneration

    if (!visibilityHooked) {
      visibilityHooked = true
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) closeStream()
        else openStream() // reconnect + resync with fresh truth
      })
    }

    let backoff = 1000
    streamController = new AbortController()
    const controller = streamController
    ;(async () => {
      while (streamGeneration === gen && !controller.signal.aborted) {
        try {
          const raw: any = await useMyFetch('/reports/activity/stream', {
            method: 'GET',
            signal: controller.signal,
            stream: true,
          } as any)
          const res: Response = (raw?.data?.value ?? raw?.data) as unknown as Response
          if (!res?.ok || !res?.body) throw new Error(`activity stream HTTP ${res?.status}`)
          backoff = 1000
          // (Re)connected: snapshot is truth for everything we track.
          fetchActivity([...trackedIds])
          const reader = res.body.getReader()
          const decoder = new TextDecoder()
          let buf = ''
          while (true) {
            const { done, value } = await reader.read()
            if (done) break
            buf += decoder.decode(value, { stream: true })
            let idx
            while ((idx = buf.indexOf('\n\n')) !== -1) {
              const frame = buf.slice(0, idx)
              buf = buf.slice(idx + 2)
              const dataLine = frame.split('\n').find(l => l.startsWith('data: '))
              if (!dataLine) continue
              try { applyEvent(JSON.parse(dataLine.slice(6))) } catch { /* skip bad frame */ }
            }
          }
        } catch { /* aborted or network error — loop decides */ }
        if (streamGeneration !== gen || controller.signal.aborted) break
        await new Promise(r => setTimeout(r, backoff))
        backoff = Math.min(backoff * 2, 15000)
      }
    })()
  }

  function closeStream() {
    streamGeneration++
    streamController?.abort()
    streamController = null
  }

  function setActiveReport(id: string | null) {
    activeReportId.value = id
  }

  return { activityMap, fetchActivity, activityFor, markReadLocal, markViewed, sortByActivity, openStream, closeStream, setActiveReport }
}
