/**
 * A `create_data` result, read as a figure for an answer.
 *
 * Nothing is decided here that `create_data` already decided: the chart type,
 * the category and measure columns, the breakdown and the units all come from
 * the inferred `data_model` and the `view` built from it
 * (backend/app/ai/tools/implementations/create_data.py,
 * build_view_from_data_model). This module only turns them into the few
 * shapes ChatFigure draws itself — horizontal bars, columns, lines, a table —
 * and says `other` for every type it leaves to the existing chart renderer.
 *
 * Pure (no Vue): unit-tested in tests/unit/figureModel.mjs.
 */
import type { InjectionKey } from 'vue'
import type { CitationSource } from './useCitations'

export type FigureKind = 'bars' | 'columns' | 'line' | 'table' | 'number' | 'other'
export type FigureFormat = 'number' | 'currency' | 'percent' | 'compact'

export interface FigureColumn { field: string; label: string; numeric: boolean }
export interface FigureSeries { name: string; values: (number | null)[] }

export interface FigureModel {
  kind: FigureKind
  /** Raw view type, for the fallback renderer. */
  type: string
  categories: string[]
  series: FigureSeries[]
  stacked: boolean
  area: boolean
  format: FigureFormat
  currency: string
  columns: FigureColumn[]
  rows: Record<string, any>[]
  /** Rows the server holds in total (the step may carry a preview). */
  totalRows: number
  /** The attached rows are a preview: load the full step before drawing. */
  partial: boolean
}

export interface FigureContext {
  /** The source a `<bow-figure source="…">` names, with its number. */
  resolve: (key: string) => CitationSource | null
  /** Open the result in the side panel (absent where there is none). */
  open?: (source: CitationSource) => void
  /** Full rows can be fetched (a signed-in report; not a shared link). */
  canHydrate: boolean
  /** The report the figures belong to (add to dashboard, save as query). */
  reportId?: string
  /** Actions that change things (add to dashboard, save as query) are offered. */
  canEdit?: boolean
}

export const FIGURE_CONTEXT: InjectionKey<FigureContext> = Symbol('bowFigureContext')

const NUMBER_TYPES = new Set(['count', 'metric_card', 'kpi', 'number', 'single_value', 'gauge'])
// Dates and periods read left to right: columns, not bars.
const TEMPORAL_RE = /^(\d{4}([-/.]\d{1,2}([-/.]\d{1,2})?)?|Q[1-4][ -]?\d{2,4}|\d{4}[ -]?Q[1-4]|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?( \d{2,4})?)([ T].*)?$/i

export function dataOf(te: any): any {
  return te?.created_step?.data || te?.result_json?.data || te?.result_json?.data_preview || null
}

function viewOf(te: any): any {
  const raw = te?.created_visualizations?.[0]?.view || te?.result_json?.view || te?.created_step?.view || null
  return raw?.view && typeof raw.view === 'object' ? raw.view : raw
}

function dataModelOf(te: any): any {
  return te?.created_step?.data_model || te?.result_json?.data_model || {}
}

const asList = (v: any): string[] => (Array.isArray(v) ? v : v ? [v] : []).map(String).filter(Boolean)

/** Read a create_data result (optionally with fully loaded `data`). */
export function figureModel(te: any, loaded?: any): FigureModel {
  const data = loaded || dataOf(te) || {}
  const rows: Record<string, any>[] = Array.isArray(data.rows) ? data.rows.filter((r: any) => r && typeof r === 'object') : []
  const declared = Array.isArray(data.columns) ? data.columns : []
  let fields = declared
    .map((c: any) => ({ field: String(c?.field ?? c?.name ?? ''), label: String(c?.headerName ?? c?.field ?? c?.name ?? '') }))
    .filter((c: any) => c.field)
  if (!fields.length && rows[0]) fields = Object.keys(rows[0]).map((k) => ({ field: k, label: k }))
  const columns: FigureColumn[] = fields.map((c: any) => ({
    ...c,
    numeric: rows.length > 0 && rows.slice(0, 50).every((r) => r[c.field] == null || typeof r[c.field] === 'number')
      && rows.some((r) => typeof r[c.field] === 'number'),
  }))
  const totals = [data.total_rows, data.row_count, te?.result_json?.stats?.total_rows].filter((n): n is number => typeof n === 'number')
  const totalRows = Math.max(rows.length, ...totals, 0)
  const partial = !loaded && (!!data.truncated || totalRows > rows.length)

  const view = viewOf(te) || {}
  const dm = dataModelOf(te)
  const type = String(view.type || dm.type || 'table').toLowerCase()
  const axisFormat = String(view.axisY?.format || dm.display?.format || '').toLowerCase()
  const format: FigureFormat = (['currency', 'percent', 'compact'].includes(axisFormat) ? axisFormat : 'number') as FigureFormat
  const currency = String(view.axisY?.currency || dm.display?.currency || 'USD').toUpperCase()

  const base: FigureModel = {
    kind: 'table', type, categories: [], series: [], stacked: !!view.stacked, area: type === 'area_chart' || !!view.area,
    format, currency, columns, rows, totalRows, partial,
  }

  // A single value is never a figure: the answer states it.
  const onlyCell = rows.length === 1 && columns.length === 1
  if (NUMBER_TYPES.has(type) || onlyCell) return { ...base, kind: 'number' }
  if (type === 'table' || !type) return base
  if (!['bar_chart', 'line_chart', 'area_chart'].includes(type)) return { ...base, kind: 'other' }

  const seriesMeta: any[] = Array.isArray(dm.series) ? dm.series : []
  const x = String(view.x || seriesMeta.find((s) => s?.key)?.key || columns.find((c) => !c.numeric)?.field || '')
  let measures = asList(view.y).length ? asList(view.y) : seriesMeta.map((s) => s?.value).filter(Boolean).map(String)
  if (!measures.length) measures = columns.filter((c) => c.numeric && c.field !== x).map((c) => c.field)
  const groupBy = String(view.groupBy || (Array.isArray(dm.group_by) ? dm.group_by[0] : dm.group_by) || '')
  if (!x || !measures.length || !rows.length) return { ...base, kind: 'other' }
  const label = (field: string) => String(
    (view.seriesStyles || []).find((s: any) => s?.value === field || s?.key === field)?.label
    || seriesMeta.find((s) => s?.value === field)?.name
    || columns.find((c) => c.field === field)?.label || field,
  )
  const num = (v: any) => (typeof v === 'number' && Number.isFinite(v) ? v : v == null || v === '' ? null : Number.isFinite(Number(v)) ? Number(v) : null)

  // Categories in the order the query returned them: the inference's sort.
  const categories: string[] = []
  for (const r of rows) { const c = String(r[x] ?? ''); if (!categories.includes(c)) categories.push(c) }
  let series: FigureSeries[]
  // A breakdown only means something when groups share categories. When every
  // category belongs to one group (month 2021-01 is only in 2021), splitting
  // by it would cut one line into disjoint pieces: draw it as one series.
  const overlaps = groupBy && groupBy !== x && rows.some((r) => groupBy in r)
    && categories.some((c) => new Set(rows.filter((r) => String(r[x] ?? '') === c).map((r) => String(r[groupBy] ?? ''))).size > 1)
  if (overlaps) {
    const groups: string[] = []
    for (const r of rows) { const g = String(r[groupBy] ?? ''); if (!groups.includes(g)) groups.push(g) }
    const measure = measures[0]
    series = groups.map((g) => ({
      name: g,
      values: categories.map((c) => {
        const hit = rows.find((r) => String(r[x] ?? '') === c && String(r[groupBy] ?? '') === g)
        return hit ? num(hit[measure]) : null
      }),
    }))
  } else {
    series = measures.map((m) => ({ name: label(m), values: categories.map((c) => num(rows.find((r) => String(r[x] ?? '') === c)?.[m])) }))
  }

  const temporal = categories.length > 1 && categories.filter((c) => TEMPORAL_RE.test(c.trim())).length / categories.length >= 0.8
  let kind: FigureKind
  if (type === 'bar_chart') kind = view.horizontal || (series.length === 1 && !temporal) ? 'bars' : 'columns'
  else kind = 'line'
  return { ...base, kind, categories, series }
}

/** Format a value the way the view says (currency, percent, compact). */
export function formatFigureValue(v: any, format: FigureFormat, currency = 'USD', locale = 'en', short = false): string {
  if (v == null || v === '') return '—'
  if (typeof v !== 'number') return String(v)
  const tag = locale === 'he' ? 'he-IL' : locale === 'es' ? 'es-ES' : 'en-US'
  try {
    if (format === 'currency') {
      // Short (axes, bar ends): whole amounts lose their cents, big ones compact.
      const digits = short ? (Number.isInteger(v) || Math.abs(v) >= 100 ? 0 : 2) : 2
      return new Intl.NumberFormat(tag, { style: 'currency', currency, notation: short && Math.abs(v) >= 10000 ? 'compact' : 'standard', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v)
    }
    if (format === 'percent') {
      // Shares arrive either as 0–1 or as 0–100.
      return new Intl.NumberFormat(tag, { style: 'percent', maximumFractionDigits: 1 }).format(Math.abs(v) > 1 ? v / 100 : v)
    }
    return new Intl.NumberFormat(tag, { notation: format === 'compact' || (short && Math.abs(v) >= 10000) ? 'compact' : 'standard', maximumFractionDigits: 2 }).format(v)
  } catch {
    return String(v)
  }
}
