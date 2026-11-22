// The data grid's column filter: pick values from a searchable list, or a
// From/To range for a numeric column with many distinct values
// (components/grid/ValueSetFilter.vue). The pure parts live here so they are
// testable without a grid.
//
// A selection is stored as the SHORTER list — the values kept (`in`) or the
// values left out (`not_in`) — so unticking one genre out of 300 doesn't store
// 299 names, and a value that only appears after a re-run is let in when the
// viewer only excluded others.
//
// The same model drives two places:
//  - a standalone grid filters its own rows (valuePasses);
//  - inside a report (chat / side panel) it becomes ONE condition of the
//    report's shared filters (toReportCondition / withColumnCondition), so the
//    chart and the Filters popover see exactly what the header filtered.

export const BLANK_KEY = '\u0000blank'
// Past this many distinct values a numeric column gets From/To, not a list.
export const MAX_LIST_FOR_NUMBERS = 50
// The list renders at most this many rows; the search narrows the rest.
export const MAX_LIST_ROWS = 200

export type ValueModel =
  | { mode: 'in' | 'not_in'; values: string[] }
  | { mode: 'range'; from: number | null; to: number | null }

export type ValueOption = { key: string; raw: any; count: number }

const isBlankValue = (v: any) => v === null || v === undefined || String(v).trim() === ''

// Values compare the way the report filter compares them (useSharedFilters'
// looseEquals): trimmed, case-folded, "7837" == 7837 == "7837.0".
export function valueKey(v: any): string {
  if (isBlankValue(v)) return BLANK_KEY
  if (typeof v === 'number') return String(v)
  const s = String(v).trim()
  const n = Number(s)
  return Number.isFinite(n) ? String(n) : s.toLowerCase()
}

// Distinct values of a column, with how many rows hold each: numbers in
// numeric order, text alphabetically, blank last.
export function collectValues(values: Iterable<any>): ValueOption[] {
  const byKey = new Map<string, ValueOption>()
  for (const v of values) {
    const key = valueKey(v)
    const seen = byKey.get(key)
    if (seen) seen.count++
    else byKey.set(key, { key, raw: isBlankValue(v) ? '' : v, count: 1 })
  }
  const list = [...byKey.values()]
  const blank = list.filter((o) => o.key === BLANK_KEY)
  const rest = list.filter((o) => o.key !== BLANK_KEY)
  const numeric = rest.length > 0 && rest.every((o) => Number.isFinite(Number(o.key)))
  rest.sort((a, b) => numeric
    ? Number(a.key) - Number(b.key)
    : String(a.raw).localeCompare(String(b.raw), undefined, { numeric: true, sensitivity: 'base' }))
  return [...rest, ...blank]
}

// A selection (the keys still ticked) as the model to store; null = no filter.
export function selectionModel(selected: Set<string>, options: ValueOption[]): ValueModel | null {
  const kept = options.filter((o) => selected.has(o.key)).map((o) => o.key)
  if (kept.length === options.length) return null
  const dropped = options.filter((o) => !selected.has(o.key)).map((o) => o.key)
  return dropped.length < kept.length
    ? { mode: 'not_in', values: dropped }
    : { mode: 'in', values: kept }
}

// Which options a stored model has ticked.
export function selectedKeys(model: ValueModel | null, options: ValueOption[]): Set<string> {
  if (!model || model.mode === 'range') return new Set(options.map((o) => o.key))
  const listed = new Set(model.values)
  return new Set(options.map((o) => o.key).filter((k) => (model.mode === 'in' ? listed.has(k) : !listed.has(k))))
}

export function isActiveModel(model: ValueModel | null | undefined): boolean {
  if (!model) return false
  if (model.mode === 'range') return model.from != null || model.to != null
  return model.mode === 'in' || model.values.length > 0
}

export function valuePasses(model: ValueModel | null, raw: any): boolean {
  if (!isActiveModel(model)) return true
  const m = model as ValueModel
  if (m.mode === 'range') {
    if (isBlankValue(raw)) return false
    const n = Number(raw)
    if (!Number.isFinite(n)) return false
    return (m.from == null || n >= m.from) && (m.to == null || n <= m.to)
  }
  const listed = m.values.includes(valueKey(raw))
  return m.mode === 'in' ? listed : !listed
}

export function matchesSearch(option: ValueOption, query: string, blankLabel: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  const label = option.key === BLANK_KEY ? blankLabel : String(option.raw)
  return label.toLowerCase().includes(q)
}

// ---- Inside a report: one condition of the shared filters ----------------

type Condition = { id: string; column: string; operator: string; value: any; value2?: any; origin?: string }
type Group = { id: string; conditions: Condition[] }

// Conditions the header wrote carry this, so the header finds (and replaces)
// its own condition and leaves the ones set in the Filters popover alone.
export const COLUMN_ORIGIN = 'column'

const newId = () => `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`

// The model as a shared-filter condition (the report evaluates `in` /
// `not_in` / `between` / `gte` / `lte` already). Blank is the empty string,
// which the report's comparison treats as equal to null.
export function toReportCondition(model: ValueModel, options: ValueOption[]): Omit<Condition, 'id' | 'column'> | null {
  if (!isActiveModel(model)) return null
  if (model.mode === 'range') {
    if (model.from != null && model.to != null) return { operator: 'between', value: model.from, value2: model.to, origin: COLUMN_ORIGIN }
    if (model.from != null) return { operator: 'gte', value: model.from, origin: COLUMN_ORIGIN }
    return { operator: 'lte', value: model.to, origin: COLUMN_ORIGIN }
  }
  const raw = new Map(options.map((o) => [o.key, o.raw]))
  return {
    operator: model.mode,
    value: model.values.map((k) => (k === BLANK_KEY ? '' : raw.has(k) ? raw.get(k) : k)),
    origin: COLUMN_ORIGIN,
  }
}

// Back from the header's own condition to a model (the popup's state).
export function fromReportCondition(cond: Condition | undefined | null): ValueModel | null {
  if (!cond) return null
  const num = (v: any) => (v === '' || v == null || !Number.isFinite(Number(v)) ? null : Number(v))
  switch (cond.operator) {
    case 'in':
    case 'not_in':
      return { mode: cond.operator, values: (Array.isArray(cond.value) ? cond.value : []).map(valueKey) }
    case 'between': return { mode: 'range', from: num(cond.value), to: num(cond.value2) }
    case 'gte': return { mode: 'range', from: num(cond.value), to: null }
    case 'lte': return { mode: 'range', from: null, to: num(cond.value) }
    default: return null
  }
}

const columnOf = (cond: Condition) => {
  const [vizId, ...rest] = (cond.column || '').split(':')
  return { vizId, field: rest.join(':') }
}

export function columnConditions(groups: Group[], vizId: string, field: string) {
  const own: Condition[] = []
  const other: Condition[] = []
  for (const g of groups || []) {
    for (const c of g.conditions || []) {
      const at = columnOf(c)
      if (at.vizId !== vizId || at.field !== field) continue
      ;(c.origin === COLUMN_ORIGIN ? own : other).push(c)
    }
  }
  return { own: own[0] || null, other }
}

// The shared filters with this column's header condition replaced (or removed
// when `cond` is null). Groups are OR'ed and conditions inside a group AND'ed,
// so a column filter — which narrows everything — goes into EVERY group of
// this visualization ((A or B) and C = (A and C) or (B and C)); with none, it
// starts its own group.
export function withColumnCondition(groups: Group[], vizId: string, field: string, cond: Omit<Condition, 'id' | 'column'> | null): Group[] {
  const isOwn = (c: Condition) => c.origin === COLUMN_ORIGIN && columnOf(c).vizId === vizId && columnOf(c).field === field
  const next: Group[] = (groups || [])
    .map((g) => ({ ...g, conditions: g.conditions.filter((c) => !isOwn(c)) }))
    .filter((g) => g.conditions.length > 0)
  if (!cond) return next
  const make = (): Condition => ({ id: newId(), column: `${vizId}:${field}`, ...cond })
  const mine = next.filter((g) => g.conditions.some((c) => columnOf(c).vizId === vizId))
  if (!mine.length) return [...next, { id: newId(), conditions: [make()] }]
  for (const g of mine) g.conditions.push(make())
  return next
}

// ---- The bridge a report host hands its grids ------------------------------
// ToolWidgetPreview provides one (provide/inject): grids under it filter
// through the report instead of on their own. Outside a report there is no
// bridge and the grid filters its own rows.
export type GridFilterBridge = {
  enabled: () => boolean                              // false: the grid filters itself
  rows: () => any[]                                   // every row, before any filter
  getModel: (field: string) => ValueModel | null      // the header's own condition
  hasOtherFilters: (field: string) => boolean         // set in the Filters popover
  isActive: (field: string) => boolean                // reactive: read in a computed
  setModel: (field: string, model: ValueModel | null, options: ValueOption[]) => void
}
export const GRID_FILTER_BRIDGE = Symbol('gridFilterBridge')

export function createReportFilterBridge(opts: {
  filters: () => Group[]
  vizId: () => string | null
  rows: () => any[]
  commit: (groups: Group[]) => void
}): GridFilterBridge {
  const at = (field: string) => columnConditions(opts.filters(), opts.vizId() || '', field)
  return {
    enabled: () => !!opts.vizId(),
    rows: opts.rows,
    getModel: (field) => fromReportCondition(at(field).own),
    hasOtherFilters: (field) => at(field).other.length > 0,
    isActive: (field) => { const c = at(field); return !!c.own || c.other.length > 0 },
    setModel: (field, model, options) => {
      const vizId = opts.vizId()
      if (!vizId) return
      const cond = model ? toReportCondition(model, options) : null
      opts.commit(withColumnCondition(opts.filters(), vizId, field, cond))
    },
  }
}
