// Owns the diagnosis explorer's state: the query string (the only page state,
// mirrored to the URL), the time range, sort, and everything fetched for the
// committed query. Parsing happens on every keystroke with no network; fetches
// happen only on commit (Enter, chip, builder, histogram click, time change,
// URL load). A stale response never overwrites a newer one.
import { tryParse, type ParseResult, type QueryError } from '~/utils/diagnosisQuery'

export interface RunItem {
  id: string
  created_at: string
  status: string
  prompt: string
  error: string | null
  platform: string
  user: { id: string | null; name: string | null; email: string | null }
  agents: string[]
  report: { id: string | null; title: string | null; turn: number | null; turns: number | null }
  completion_id: string | null
  model: string | null
  provider: string | null
  tools: { total: number; failed: number }
  duration_ms: number | null
  tokens: number | null
  cost_usd: number | null
  cost_is_partial: boolean
  judge: { confidence: number | null; instructions: number | null; context: number | null }
  feedback: 'positive' | 'negative' | 'none'
  feedback_message: string | null
  eval: boolean
  matched_tool_call_ids: string[]
}

export interface ToolCall {
  id: string
  tool: string
  action: string | null
  status: string
  attempt: number
  max_retries: number
  duration_ms: number | null
  started_at: string | null
  error: string | null
  result_summary: string | null
}

export interface Bucket { bucket: string; total: number; matched: number; matched_errors: number }
export interface ToolStat { tool: string; calls: number; errors: number; avg_ms: number | null }
export interface Summary { matched: number; errors: number; users: number; cost_usd: number; p50_ms: number | null }
export interface Facet { value: string; label: string; count: number }

export type RangePreset = '24h' | '7d' | '30d' | '90d' | 'custom'
export interface TimeRange { preset: RangePreset; start: Date; end: Date }

const PRESET_MS: Record<Exclude<RangePreset, 'custom'>, number> = {
  '24h': 24 * 3600e3, '7d': 7 * 86400e3, '30d': 30 * 86400e3, '90d': 90 * 86400e3,
}

export function rangeForPreset(preset: RangePreset, custom?: { start: Date; end: Date }): TimeRange {
  if (preset === 'custom' && custom) return { preset, start: custom.start, end: custom.end }
  const key = preset === 'custom' ? '30d' : preset
  const end = new Date()
  return { preset: key, start: new Date(end.getTime() - PRESET_MS[key]), end }
}

function encodeRange(r: TimeRange): string {
  if (r.preset !== 'custom') return r.preset
  return `${r.start.toISOString().slice(0, 10)}..${r.end.toISOString().slice(0, 10)}`
}

function decodeRange(s: string | undefined): TimeRange {
  if (!s) return rangeForPreset('30d')
  if (s in PRESET_MS) return rangeForPreset(s as RangePreset)
  const m = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/.exec(s)
  if (m) {
    const start = new Date(m[1] + 'T00:00:00')
    const end = new Date(m[2] + 'T23:59:59.999')
    if (!isNaN(start.getTime()) && !isNaN(end.getTime()) && end > start) return { preset: 'custom', start, end }
  }
  return rangeForPreset('30d')
}

export const useDiagnosisQuery = () => {
  const route = useRoute()
  const router = useRouter()

  // --- state ---------------------------------------------------------------
  const text = ref<string>(String(route.query.q ?? ''))          // what is in the bar
  const committed = ref<string>(text.value)                        // what the panels show
  const range = ref<TimeRange>(decodeRange(route.query.range as string | undefined))
  const sort = ref<string>(String(route.query.sort ?? 'created'))
  const dir = ref<'asc' | 'desc'>((route.query.dir as 'asc' | 'desc') ?? 'desc')

  const items = ref<RunItem[]>([])
  const nextCursor = ref<string | null>(null)
  const total = ref(0)
  const totalInRange = ref(0)
  const summary = ref<Summary | null>(null)
  const buckets = ref<Bucket[]>([])
  const granularity = ref<'hour' | 'day' | 'week'>('day')
  const tools = ref<ToolStat[]>([])
  const canonical = ref('')
  const loading = ref(false)
  const loadingMore = ref(false)
  const serverError = ref<string | null>(null)
  const lastMs = ref<number | null>(null)

  // --- parse on every keystroke (zero network) ----------------------------
  const parsed = computed<{ result?: ParseResult; error?: QueryError }>(() => tryParse(text.value))
  const parseError = computed(() => parsed.value.error ?? null)
  const tokens = computed(() => parsed.value.result?.tokens ?? [])

  // --- fetch plumbing ------------------------------------------------------
  let seq = 0
  const tz = -new Date().getTimezoneOffset()

  const baseParams = (q: string) => ({
    q,
    start: range.value.start.toISOString(),
    end: range.value.end.toISOString(),
    tz: String(tz),
  })

  const encode = (params: Record<string, string>) => new URLSearchParams(params).toString()

  const syncUrl = () => {
    const query: Record<string, string> = {}
    if (committed.value) query.q = committed.value
    const r = encodeRange(range.value)
    if (r !== '30d') query.range = r
    if (sort.value !== 'created') query.sort = sort.value
    if (dir.value !== 'desc') query.dir = dir.value
    router.replace({ query })
  }

  const run = async () => {
    const q = text.value
    if (tryParse(q).error) return
    committed.value = q
    syncUrl()
    const mine = ++seq
    loading.value = true
    serverError.value = null
    const started = performance.now()
    try {
      const url = `/api/console/diagnosis/runs?${encode({ ...baseParams(q), sort: sort.value, dir: dir.value, limit: '25' })}`
      const res = await useMyFetch<any>(url)
      if (mine !== seq) return
      if (res.error.value) {
        const detail = (res.error.value as any)?.data?.detail
        serverError.value = detail?.message || 'Request failed'
        return
      }
      const body = res.data.value
      items.value = body.items
      nextCursor.value = body.next_cursor
      total.value = body.total
      totalInRange.value = body.total_in_range
      summary.value = body.summary
      buckets.value = body.histogram.buckets
      granularity.value = body.histogram.granularity
      tools.value = body.tools
      canonical.value = body.query.canonical
      lastMs.value = Math.round(performance.now() - started)
    } finally {
      if (mine === seq) loading.value = false
    }
  }

  const loadMore = async () => {
    if (!nextCursor.value || loadingMore.value) return
    loadingMore.value = true
    const mine = seq
    try {
      const url = `/api/console/diagnosis/runs?${encode({ ...baseParams(committed.value), sort: sort.value, dir: dir.value, limit: '25', cursor: nextCursor.value, include: 'items' })}`
      const res = await useMyFetch<any>(url)
      if (mine !== seq || res.error.value) return
      items.value = items.value.concat(res.data.value.items)
      nextCursor.value = res.data.value.next_cursor
    } finally {
      loadingMore.value = false
    }
  }

  // Facet lookups: debounced by the caller; cached per (field, prefix, range, q).
  const facetCache = new Map<string, Facet[]>()
  const facets = async (field: string, prefix = '', qWithoutTerm = committed.value): Promise<Facet[]> => {
    const key = `${field}|${prefix}|${encodeRange(range.value)}|${qWithoutTerm}`
    const hit = facetCache.get(key)
    if (hit) return hit
    const url = `/api/console/diagnosis/facets/${encodeURIComponent(field)}?${encode({ ...baseParams(qWithoutTerm), prefix })}`
    const res = await useMyFetch<Facet[]>(url)
    const out = res.error.value ? [] : (res.data.value as Facet[])
    facetCache.set(key, out)
    return out
  }

  const toolCalls = async (runIds: string[]): Promise<Record<string, ToolCall[]>> => {
    if (!runIds.length) return {}
    const res = await useMyFetch<Record<string, ToolCall[]>>(`/api/console/diagnosis/runs/tool_calls?run_ids=${runIds.join(',')}`)
    return res.error.value ? {} : (res.data.value as Record<string, ToolCall[]>)
  }

  const fieldsMeta = ref<any>(null)
  const loadFields = async () => {
    if (fieldsMeta.value) return fieldsMeta.value
    const res = await useMyFetch<any>('/api/console/diagnosis/fields')
    fieldsMeta.value = res.error.value ? null : res.data.value
    return fieldsMeta.value
  }

  // --- commits from the page -----------------------------------------------
  const setText = (v: string) => { text.value = v }
  const commit = (v: string) => { text.value = v; return run() }
  const setRange = (r: TimeRange) => { range.value = r; facetCache.clear(); return run() }
  const setSort = (s: string, d: 'asc' | 'desc') => { sort.value = s; dir.value = d; return run() }

  return {
    text, committed, range, sort, dir,
    items, nextCursor, total, totalInRange, summary, buckets, granularity, tools, canonical,
    loading, loadingMore, serverError, lastMs, parseError, tokens, tz,
    run, loadMore, facets, toolCalls, loadFields, fieldsMeta,
    setText, commit, setRange, setSort,
  }
}

// --- formatting shared by the components --------------------------------------
export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  if (ms < 3_600_000) return `${(ms / 60_000).toFixed(1)}m`
  return `${(ms / 3_600_000).toFixed(1)}h`
}

export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`
  return `${(n / 1_000_000).toFixed(2)}m`
}

export function fmtCost(usd: number | null | undefined, partial = false): string {
  if (usd == null) return '—'
  const s = usd < 0.01 && usd > 0 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`
  return partial ? `~${s}` : s
}

export function fmtWhen(iso: string | null | undefined, locale = 'en'): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(d)
}

export function fmtTime(iso: string | null | undefined, locale = 'en'): string {
  if (!iso) return '—'
  return new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).format(new Date(iso))
}
