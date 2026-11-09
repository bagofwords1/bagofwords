/*
 * Turns the flat sidebar report list into the ordered groups the rail renders,
 * driven by the saved view (useSidebarReportsView):
 *
 *   - Starred reports are pinned at the top in their own "Starred" group,
 *     whatever the grouping (except in `none`, which is a flat list like before
 *     — starred first, no headers).
 *   - Sorting (updated / created / name, asc / desc) applies *inside* each
 *     group; group order itself is fixed.
 *   - Date grouping buckets by the field being sorted on (updated or created);
 *     Name sort buckets by updated. Status grouping reads the live activity map
 *     so a report moves to "Running" the moment its run starts, no reload.
 *
 * Reads `activityMap` (the SSE-backed shared store) so both the sort and the
 * status buckets re-evaluate when stream events arrive.
 */

type MaybeReport = {
  id: string
  title?: string | null
  is_starred?: boolean
  created_at?: string | null
  last_activity_at?: string | null
  [k: string]: any
}

export interface SidebarReportGroup {
  key: string
  label: string
  // `none` renders a single header-less group that behaves like today's list.
  flat?: boolean
  reports: MaybeReport[]
}

const DATE_BUCKET_ORDER = ['today', 'yesterday', 'previous7', 'previous30', 'older'] as const
const STATUS_BUCKET_ORDER = ['needsInput', 'running', 'queued', 'idle'] as const

export function useReportGrouping(reports: Ref<MaybeReport[]>) {
  const { groupBy, sortBy, direction } = useSidebarReportsView()
  const { activityMap } = useReportActivity()
  const { t, locale } = useI18n()

  // Updated timestamp, preferring live stream truth when it is newer than the
  // fetched row (mirrors sortByActivity in useReportActivity).
  const updatedTs = (r: MaybeReport): string => {
    const live = activityMap.value[r.id]?.last_activity_at
    const base = r.last_activity_at || r.created_at || ''
    return (live && live > base ? live : base) || ''
  }
  const createdTs = (r: MaybeReport): string => r.created_at || ''
  // Which timestamp a date bucket is decided by: created only when sorting by
  // created; updated for updated *and* name sort.
  const bucketTs = (r: MaybeReport): string =>
    sortBy.value === 'created' ? createdTs(r) : updatedTs(r)

  const sorted = computed<MaybeReport[]>(() => {
    const dir = direction.value === 'asc' ? 1 : -1
    const list = [...reports.value]
    list.sort((a, b) => {
      let cmp: number
      if (sortBy.value === 'name') {
        cmp = (a.title || '').localeCompare(b.title || '', locale.value, { sensitivity: 'base' })
      } else if (sortBy.value === 'created') {
        cmp = createdTs(a).localeCompare(createdTs(b))
      } else {
        cmp = updatedTs(a).localeCompare(updatedTs(b))
      }
      // Stable, deterministic tiebreak so equal keys don't jitter between renders.
      if (cmp === 0) cmp = (a.id || '').localeCompare(b.id || '')
      return cmp * dir
    })
    return list
  })

  const grouped = computed<SidebarReportGroup[]>(() => {
    const list = sorted.value

    if (groupBy.value === 'none') {
      const starred = list.filter(r => r.is_starred)
      const rest = list.filter(r => !r.is_starred)
      return [{ key: 'all', label: '', flat: true, reports: [...starred, ...rest] }]
    }

    const starred = list.filter(r => r.is_starred)
    const rest = list.filter(r => !r.is_starred)
    const groups: SidebarReportGroup[] = []
    if (starred.length) {
      groups.push({ key: 'starred', label: t('reports.view.groups.starred'), reports: starred })
    }

    if (groupBy.value === 'date') {
      const now = new Date()
      const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
      const DAY = 86400000
      const buckets: Record<string, MaybeReport[]> = {
        today: [], yesterday: [], previous7: [], previous30: [], older: [],
      }
      for (const r of rest) {
        const t0 = Date.parse(bucketTs(r))
        let b = 'older'
        if (!Number.isNaN(t0)) {
          if (t0 >= startToday) b = 'today'
          else if (t0 >= startToday - DAY) b = 'yesterday'
          else if (t0 >= startToday - 7 * DAY) b = 'previous7'
          else if (t0 >= startToday - 30 * DAY) b = 'previous30'
        }
        buckets[b].push(r)
      }
      for (const key of DATE_BUCKET_ORDER) {
        if (buckets[key].length) groups.push({ key, label: t(`reports.view.groups.${key}`), reports: buckets[key] })
      }
    } else {
      // status
      const buckets: Record<string, MaybeReport[]> = {
        needsInput: [], running: [], queued: [], idle: [],
      }
      for (const r of rest) {
        const state = activityMap.value[r.id]?.state
        const b = state === 'awaiting_user' ? 'needsInput'
          : state === 'running' ? 'running'
          : state === 'queued' ? 'queued'
          : 'idle'
        buckets[b].push(r)
      }
      for (const key of STATUS_BUCKET_ORDER) {
        if (buckets[key].length) groups.push({ key, label: t(`reports.view.groups.${key}`), reports: buckets[key] })
      }
    }

    return groups
  })

  return { grouped }
}
