// How a viewer arranged a dashboard's DataTable — column widths they set by
// hand and the order they dragged the columns into — remembered in THIS
// browser (see DataTable in public/libs/artifact-globals.js).
//
// The dashboard runs in a sandboxed iframe with an opaque origin — it has no
// localStorage of its own — so it posts each change to the host page
// (ARTIFACT_TABLE_LAYOUT) and the host hands the saved layouts back with the
// data (ARTIFACT_DATA payload `table_layouts`). The iframe runs generated
// code, so everything it sends is validated and capped before it is stored:
// per table, a bounded map of column name -> whole pixel width and a bounded
// list of column names.
//
// Per viewer, per browser: arranging a shared dashboard changes nothing for
// anyone else, and nothing reaches the server. Every access is guarded —
// private windows and blocked storage throw — and a failure just means the
// table as it was built.

export const TABLE_LAYOUT_MESSAGE = 'ARTIFACT_TABLE_LAYOUT'
const STORAGE_KEY = 'bow.artifactTableLayouts.v1'
const MAX_DASHBOARDS = 100
const MAX_TABLES = 20          // per dashboard
const MAX_COLUMNS = 100        // per table
const MAX_KEY = 120            // characters, table key and column name
// Byte budgets: the frame's code is generated, so it must not be able to fill
// the origin's localStorage (5MB, shared with every other bow.* setting).
const MAX_DASHBOARD_BYTES = 32 * 1024
const MAX_TOTAL_BYTES = 256 * 1024
const MIN_WIDTH = 40
const MAX_WIDTH = 4000

export type TableLayout = { widths: Record<string, number>; order: string[] }
export type TableLayouts = Record<string, TableLayout>   // table key -> layout
type Entry = { t: number; tables: TableLayouts }

function readAll(): Record<string, Entry> {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

const okName = (s: unknown): s is string => typeof s === 'string' && s.length > 0 && s.length <= MAX_KEY

// Widths from untrusted input: only column names and sane numbers.
export function cleanWidths(input: unknown): Record<string, number> | null {
  if (input == null) return {}
  if (typeof input !== 'object' || Array.isArray(input)) return null
  const out: Record<string, number> = {}
  let n = 0
  for (const [col, w] of Object.entries(input as Record<string, unknown>)) {
    if (n >= MAX_COLUMNS) break
    if (!okName(col) || typeof w !== 'number' || !Number.isFinite(w)) continue
    out[col] = Math.round(Math.min(Math.max(w, MIN_WIDTH), MAX_WIDTH))
    n++
  }
  return out
}

// An order from untrusted input: distinct column names, capped.
export function cleanOrder(input: unknown): string[] | null {
  if (input == null) return []
  if (!Array.isArray(input)) return null
  const seen = new Set<string>()
  for (const f of input) {
    if (seen.size >= MAX_COLUMNS) break
    if (okName(f)) seen.add(f)
  }
  return [...seen]
}

export function cleanLayout(widths: unknown, order: unknown): TableLayout | null {
  const w = cleanWidths(widths)
  const o = cleanOrder(order)
  return w && o ? { widths: w, order: o } : null
}

const isEmpty = (l: TableLayout) => !Object.keys(l.widths).length && !l.order.length

export function loadTableLayouts(dashboardId: string | null | undefined): TableLayouts {
  if (!dashboardId) return {}
  const tables = readAll()[dashboardId]?.tables
  if (!tables || typeof tables !== 'object') return {}
  const out: TableLayouts = {}
  for (const [key, l] of Object.entries(tables).slice(0, MAX_TABLES)) {
    const clean = l && typeof l === 'object' ? cleanLayout((l as any).widths, (l as any).order) : null
    if (clean && !isEmpty(clean)) out[key] = clean
  }
  return out
}

// Store one table's layout (an empty one forgets the table: back to as built).
export function saveTableLayout(dashboardId: string | null | undefined, tableKey: unknown, widths: unknown, order: unknown): boolean {
  if (!dashboardId || !okName(tableKey)) return false
  const clean = cleanLayout(widths, order)
  if (!clean) return false
  try {
    const all = readAll()
    const tables: TableLayouts = { ...(all[dashboardId]?.tables || {}) }
    if (isEmpty(clean)) delete tables[tableKey]
    else tables[tableKey] = clean
    const keys = Object.keys(tables)
    if (keys.length > MAX_TABLES) keys.slice(0, keys.length - MAX_TABLES).forEach((k) => delete tables[k])
    const entry = { t: Date.now(), tables }
    if (JSON.stringify(entry).length > MAX_DASHBOARD_BYTES) return false
    all[dashboardId] = entry
    // Oldest dashboards go first: past the count, then past the byte budget.
    const oldestFirst = Object.keys(all).sort((a, b) => (all[a]?.t || 0) - (all[b]?.t || 0))
    while (oldestFirst.length > MAX_DASHBOARDS) delete all[oldestFirst.shift() as string]
    let json = JSON.stringify(all)
    while (json.length > MAX_TOTAL_BYTES && oldestFirst.length > 1) {
      delete all[oldestFirst.shift() as string]
      json = JSON.stringify(all)
    }
    localStorage.setItem(STORAGE_KEY, json)
    return true
  } catch {
    return false
  }
}

// A host's message handler: true when the message was a layout update (the
// caller has already checked event.source is its own dashboard frame).
export function handleTableLayoutMessage(data: any, dashboardId: string | null | undefined): boolean {
  if (!data || data.type !== TABLE_LAYOUT_MESSAGE) return false
  saveTableLayout(dashboardId, data.key, data.widths, data.order)
  return true
}
