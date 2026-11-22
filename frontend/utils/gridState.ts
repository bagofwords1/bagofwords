// Per-viewer memory of what a viewer did to a data grid's columns (the
// order after a drag, the sort, hand-resized widths) — see AgGridComponent.
//
// It lives in this browser only: a viewer arranging a shared dashboard's table
// changes nothing for anyone else, and nothing reaches the server. One
// localStorage entry holds every grid, keyed by the caller's stable key
// (widget / query id), and keeps the most recently touched MAX_GRIDS so it
// can't grow without bound. Every access is guarded — private windows and
// blocked storage throw — and a failure just means the default layout.

// v2: a layout object, not a full column-state snapshot (v1 froze automatic
// widths as if chosen; it is simply ignored).
const STORAGE_KEY = 'bow.gridState.v2'
const MAX_GRIDS = 200

type Entry = { t: number; s: Record<string, any> }

function readAll(): Record<string, Entry> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

export function loadGridState(key: string): Record<string, any> | null {
  if (!key) return null
  const s = readAll()[key]?.s
  if (!s || typeof s !== 'object' || Array.isArray(s)) return null
  return {
    order: Array.isArray(s.order) ? s.order.filter((x: unknown) => typeof x === 'string') : [],
    sort: Array.isArray(s.sort) ? s.sort.filter((x: any) => x && typeof x.colId === 'string' && (x.sort === 'asc' || x.sort === 'desc')) : [],
    widths: s.widths && typeof s.widths === 'object' && !Array.isArray(s.widths)
      ? Object.fromEntries(Object.entries(s.widths).filter(([, w]) => typeof w === 'number' && w > 0))
      : {},
  }
}

export function saveGridState(key: string, state: Record<string, any>): void {
  if (!key || !state || typeof state !== 'object') return
  try {
    const all = readAll()
    all[key] = { t: Date.now(), s: state }
    const keys = Object.keys(all)
    if (keys.length > MAX_GRIDS) {
      keys
        .sort((a, b) => (all[a].t || 0) - (all[b].t || 0))
        .slice(0, keys.length - MAX_GRIDS)
        .forEach((k) => delete all[k])
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(all))
  } catch {
    /* storage full or blocked: keep the in-memory layout only */
  }
}
