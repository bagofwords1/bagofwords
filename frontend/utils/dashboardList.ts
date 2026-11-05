/**
 * The dashboards page's list state, its URL and its API params, and what a
 * card says about itself (dashboards findability, P8).
 *
 * Pure: no Vue, no i18n, no fetch. The page wires these to the router and to
 * GET /api/artifacts; the card components translate the keys they return.
 * Unit-tested by tests/unit/dashboardList.mjs.
 */

export const SORTS = ['edited', 'created', '-created', 'title', 'published'] as const
export const PRESETS = ['any', '7', '30', '90', 'year', 'older90', 'custom'] as const
export const STATUSES = ['any', 'published', 'changes', 'draft'] as const
export const ACCESS_CHIPS = ['all', 'direct', 'group', 'project', 'org', 'edit'] as const
export const TYPES = ['all', 'page', 'doc'] as const

export type DashboardSort = typeof SORTS[number]
export type DatePreset = typeof PRESETS[number]
export type DashboardStatus = typeof STATUSES[number]
export type AccessChip = typeof ACCESS_CHIPS[number]
export type DashboardType = typeof TYPES[number]
export type DashboardTab = 'my' | 'shared'

export interface DashboardListState {
  tab: DashboardTab
  q: string
  type: DashboardType
  access: AccessChip
  agents: string[]
  owners: string[]
  created: DatePreset
  createdFrom: string // YYYY-MM-DD, with created = 'custom'
  createdTo: string
  edited: DatePreset
  editedFrom: string
  editedTo: string
  status: DashboardStatus
  starred: boolean
  attention: boolean
  sort: DashboardSort
  page: number
}

/** One card of GET /api/artifacts (backend DashboardListItem). */
export interface DashboardCardData {
  id: string
  slug?: string | null
  title?: string | null
  mode: string
  published: boolean
  is_owner?: boolean
  role: string // 'owner' | 'edit' | 'view'
  report_id?: string | null
  owner?: { id: string; name?: string | null } | null
  thumbnail_url?: string | null
  created_at?: string | null
  edited_at?: string | null
  published_at?: string | null
  visibility?: string | null
  access?: { reasons: string[]; primary?: string | null; via_name?: string | null } | null
  is_starred?: boolean
  is_new?: boolean
  // For the card's editors only; null for a view-only reader.
  unpublished_changes?: boolean | null
  cron_schedule?: string | null
  last_run_status?: string | null
  last_run_at?: string | null
  agents?: Array<{ id: string; name?: string | null; type?: string | null; icon_token?: string | null }>
}

export interface FacetAgent { id: string; name?: string | null; type?: string | null; icon_token?: string | null }

/** GET /api/artifacts/facets (backend DashboardListFacets): counts are upper bounds. */
export interface DashboardFacets {
  tabs: { my: number; shared: number }
  access?: Record<AccessChip, number> | null
  agents?: Array<FacetAgent & { count: number }>
  owners?: Array<{ id: string; name?: string | null; count: number }>
  status?: { published: number; changes: number; draft: number } | null
  new_count?: number
  attention?: { failing: number; stale: number; total: number } | null
}

/** Agents and owners the page has seen in facets, by id: a selected one keeps its name at count 0. */
export interface FacetNames {
  agents: Record<string, FacetAgent>
  owners: Record<string, string>
}

export const DEFAULT_STATE: DashboardListState = Object.freeze({
  tab: 'my',
  q: '',
  type: 'all',
  access: 'all',
  agents: [],
  owners: [],
  created: 'any',
  createdFrom: '',
  createdTo: '',
  edited: 'any',
  editedFrom: '',
  editedTo: '',
  status: 'any',
  starred: false,
  attention: false,
  sort: 'edited',
  page: 1,
}) as DashboardListState

const PAGE_SIZE = { grid: 15, list: 50 } as const
const MAX_IDS = 100
const MAX_SEARCH = 200
const DAY_MS = 864e5

// --- URL -----------------------------------------------------------------------

function first(value: unknown): string | undefined {
  const v = Array.isArray(value) ? value[0] : value
  return typeof v === 'string' ? v : undefined
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  const v = first(value)
  return v !== undefined && (allowed as readonly string[]).includes(v) ? (v as T) : fallback
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** Comma-separated ids: each kept only if UUID-shaped (lowercased, as the API stores it). */
function idList(value: unknown): string[] {
  const v = first(value)
  if (!v) return []
  const ids = v.split(',').map(s => s.trim()).filter(s => UUID.test(s)).map(s => s.toLowerCase())
  return [...new Set(ids)].slice(0, MAX_IDS)
}

function flag(value: unknown): boolean {
  const v = first(value)
  return v === '1' || v === 'true'
}

/**
 * A real calendar date as YYYY-MM-DD, else null. The year stops at 9998: a
 * custom `to` is sent as 00:00 of the next day, and 9999-12-31 has none
 * that a four-digit year can write.
 */
function calendarDate(value: unknown): { y: number; m: number; d: number } | null {
  const v = first(value)
  const match = v && /^(\d{4})-(\d{2})-(\d{2})$/.exec(v)
  if (!match) return null
  const [y, m, d] = [Number(match[1]), Number(match[2]), Number(match[3])]
  if (y > 9998) return null
  const probe = new Date(Date.UTC(y, m - 1, d))
  if (probe.getUTCFullYear() !== y || probe.getUTCMonth() !== m - 1 || probe.getUTCDate() !== d) return null
  return { y, m, d }
}

function validDate(value: unknown): string {
  return calendarDate(value) ? (first(value) as string) : ''
}

/** A preset name wins; else valid custom dates make a custom range. */
function parseRange(query: Record<string, unknown>, key: 'created' | 'edited') {
  const preset = oneOf(query[key], PRESETS, 'any')
  if (preset !== 'any' && preset !== 'custom') return { preset, from: '', to: '' }
  const from = validDate(query[`${key}_from`])
  const to = validDate(query[`${key}_to`])
  // Both ends are inclusive days, so from = to is one day; from after to is no range.
  if ((!from && !to) || (from && to && from > to)) return { preset: 'any' as DatePreset, from: '', to: '' }
  return { preset: 'custom' as DatePreset, from, to }
}

/** The route's query as a list state. Unknown or invalid values are dropped, each on its own. */
export function parseListQuery(query: Record<string, unknown>): DashboardListState {
  const tab = oneOf(query.tab, ['my', 'shared'] as const, 'my')
  const created = parseRange(query, 'created')
  const edited = parseRange(query, 'edited')
  let sort = oneOf(query.sort, SORTS, 'edited')
  if (sort === 'published' && tab !== 'shared') sort = 'edited' // a Shared sort
  const page = first(query.page) ?? ''
  return {
    tab,
    // By code point, as the API's max_length counts: a surrogate pair is never cut.
    q: Array.from(first(query.q) ?? '').slice(0, MAX_SEARCH).join(''),
    type: oneOf(query.type, TYPES, 'all'),
    access: oneOf(query.access, ACCESS_CHIPS, 'all'),
    agents: idList(query.agents),
    owners: idList(query.owners),
    created: created.preset,
    createdFrom: created.from,
    createdTo: created.to,
    edited: edited.preset,
    editedFrom: edited.from,
    editedTo: edited.to,
    status: oneOf(query.status, STATUSES, 'any'),
    starred: flag(query.starred),
    attention: flag(query.attention),
    sort,
    page: /^\d{1,6}$/.test(page) && Number(page) >= 1 ? Number(page) : 1,
  }
}

/** The list state as a route query, without the defaults. A custom range is its plain (valid) dates. */
export function toRouteQuery(state: DashboardListState): Record<string, string> {
  const out: Record<string, string> = {}
  if (state.tab !== DEFAULT_STATE.tab) out.tab = state.tab
  if (state.q) out.q = state.q
  if (state.type !== DEFAULT_STATE.type) out.type = state.type
  if (state.access !== DEFAULT_STATE.access) out.access = state.access
  if (state.agents.length) out.agents = state.agents.join(',')
  if (state.owners.length) out.owners = state.owners.join(',')
  for (const key of ['created', 'edited'] as const) {
    const preset = state[key]
    if (preset === 'custom') {
      // Only a real calendar date: the URL never carries what parseListQuery would drop.
      const from = validDate(state[`${key}From`])
      const to = validDate(state[`${key}To`])
      if (from) out[`${key}_from`] = from
      if (to) out[`${key}_to`] = to
    } else if (preset !== 'any') {
      out[key] = preset
    }
  }
  if (state.status !== DEFAULT_STATE.status) out.status = state.status
  if (state.starred) out.starred = '1'
  if (state.attention) out.attention = '1'
  if (state.sort !== DEFAULT_STATE.sort) out.sort = state.sort
  if (state.page !== DEFAULT_STATE.page) out.page = String(state.page)
  return out
}

// --- dates -------------------------------------------------------------------------

/** The zone's offset from UTC at `instant`, in ms (local wall time − UTC). */
function zoneOffset(instant: number, timeZone?: string): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(new Date(instant))
  const n = (type: string) => Number(parts.find(p => p.type === type)?.value)
  const wall = Date.UTC(n('year'), n('month') - 1, n('day'), n('hour'), n('minute'), n('second'))
  return wall - Math.floor(instant / 1000) * 1000
}

/** The calendar day (YYYY-MM-DD) `instant` falls on in `timeZone`. */
function zonedDate(instant: number, timeZone?: string): string {
  return new Date(instant + zoneOffset(instant, timeZone)).toISOString().slice(0, 10)
}

/**
 * The first instant of `date` (YYYY-MM-DD) in `timeZone` (undefined: the
 * runtime's zone), or null when `date` is not a calendar date. Two
 * candidates: 00:00 by the offset at UTC midnight of that date, and by the
 * offset at that first guess (a DST change between the two moves it). Of
 * those that fall on `date` in the zone, the earliest: where 00:00 itself is
 * skipped (Chile springs forward at midnight), the day starts at 01:00.
 */
export function zonedStartOfDay(date: string, timeZone?: string): Date | null {
  const c = calendarDate(date)
  if (!c) return null
  const utcMidnight = Date.UTC(c.y, c.m - 1, c.d)
  const guess = utcMidnight - zoneOffset(utcMidnight, timeZone)
  const recheck = utcMidnight - zoneOffset(guess, timeZone)
  const onDate = [guess, recheck].filter(instant => zonedDate(instant, timeZone) === date)
  return new Date(onDate.length ? Math.min(...onDate) : recheck)
}

function nextDay(date: string): string {
  const c = calendarDate(date)!
  return new Date(Date.UTC(c.y, c.m - 1, c.d + 1)).toISOString().slice(0, 10)
}

function yearIn(now: Date, timeZone?: string): number {
  return Number(new Intl.DateTimeFormat('en-US', { timeZone, year: 'numeric' }).format(now))
}

/**
 * A date filter as API bounds (ISO, `Z`): `from` inclusive, `to` exclusive.
 * Last N days and "older than 90 days" are rolling (now − N × 24 h). "This
 * year" and custom dates are days in `timeZone`; a custom `to` is 00:00 of
 * the next day, so the picked day is included whole.
 */
export function presetRange(
  preset: string,
  now: Date,
  opts: { from?: string; to?: string; timeZone?: string },
): { from?: string; to?: string } {
  const ago = (days: number) => new Date(now.getTime() - days * DAY_MS).toISOString()
  switch (preset) {
    case '7':
    case '30':
    case '90':
      return { from: ago(Number(preset)) }
    case 'older90':
      return { to: ago(90) }
    case 'year': {
      const from = zonedStartOfDay(`${yearIn(now, opts.timeZone)}-01-01`, opts.timeZone)
      return from ? { from: from.toISOString() } : {}
    }
    case 'custom': {
      const out: { from?: string; to?: string } = {}
      const from = opts.from ? zonedStartOfDay(opts.from, opts.timeZone) : null
      const next = opts.to && calendarDate(opts.to) ? nextDay(opts.to) : null
      const to = next ? zonedStartOfDay(next, opts.timeZone) : null
      if (from) out.from = from.toISOString()
      if (to) out.to = to.toISOString()
      return out
    }
    default:
      return {}
  }
}

/**
 * Whether the state's API params depend on the time zone: "This year" or a
 * custom range with a date. Rolling presets ("Last 7 days") do not.
 */
export function usesZonedDates(state: DashboardListState): boolean {
  return (['created', 'edited'] as const).some(key => state[key] === 'year'
    || (state[key] === 'custom' && !!(state[`${key}From`] || state[`${key}To`])))
}

// --- API -----------------------------------------------------------------------------

/**
 * `timeZone` when this runtime's Intl knows it, else undefined (the runtime's
 * zone): an org zone from a newer tz database must not break the page.
 */
function knownTimeZone(timeZone?: string): string | undefined {
  if (!timeZone) return undefined
  try {
    new Intl.DateTimeFormat('en-US', { timeZone })
    return timeZone
  } catch {
    return undefined
  }
}

/**
 * GET /api/artifacts params for a state. Filters the API ignores on a tab are
 * left out there: `status` on Shared, `access` on My (and the owners and
 * `new_since`, which mean nothing on My, where every card is the reader's).
 */
export function toApiParams(
  state: DashboardListState,
  now: Date,
  opts: { view: 'grid' | 'list'; newSince?: string | null; timeZone?: string },
): Record<string, string | number | boolean> {
  const shared = state.tab === 'shared'
  const timeZone = knownTimeZone(opts.timeZone)
  const out: Record<string, string | number | boolean> = {
    filter: state.tab,
    page: state.page,
    limit: PAGE_SIZE[opts.view],
    sort: state.sort,
  }
  const search = state.q.trim()
  if (search) out.search = search
  if (state.type !== 'all') out.mode = state.type
  if (shared && state.access !== 'all') out.access = state.access
  if (state.agents.length) out.agent_ids = state.agents.join(',')
  if (shared && state.owners.length) out.owner_ids = state.owners.join(',')
  for (const key of ['created', 'edited'] as const) {
    const range = presetRange(state[key], now, {
      from: state[`${key}From`],
      to: state[`${key}To`],
      timeZone,
    })
    if (range.from) out[`${key}_from`] = range.from
    if (range.to) out[`${key}_to`] = range.to
  }
  if (!shared && state.status !== 'any') out.status = state.status
  if (state.starred) out.starred = true
  if (state.attention) out.attention = true
  if (shared && opts.newSince) out.new_since = opts.newSince
  return out
}

/**
 * GET /api/artifacts/facets params: the list's filters, without paging or
 * sort; `new_since` on both tabs, since the Shared tab's "{n} new" shows on
 * My too.
 */
export function toFacetParams(
  state: DashboardListState,
  now: Date,
  opts: { newSince?: string | null; timeZone?: string },
): Record<string, string | number | boolean> {
  const { page: _page, limit: _limit, sort: _sort, ...out } = toApiParams(state, now, { view: 'grid', timeZone: opts.timeZone })
  if (opts.newSince) out.new_since = opts.newSince
  return out
}

// --- the page's history (Durable decisions, "History") -------------------------------

/** The route query keys the page owns. Any other key (Excel's `excel`) is left alone. */
export const ROUTE_KEYS = [
  'tab', 'q', 'type', 'access', 'agents', 'owners', 'created', 'created_from', 'created_to',
  'edited', 'edited_from', 'edited_to', 'status', 'starred', 'attention', 'sort', 'page',
] as const

/** The route query for `state`: the page's keys rewritten, every other key of `current` kept. */
export function withListQuery(current: Record<string, unknown>, state: DashboardListState): Record<string, unknown> {
  const own = new Set<string>(ROUTE_KEYS)
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(current)) if (!own.has(key)) out[key] = value
  return { ...out, ...toRouteQuery(state) }
}

/**
 * Whether the page's keys in `query` are exactly what it would write for
 * them. When not (an unknown or invalid value, a default written out), the
 * page replaces the URL once on load, so Back never returns to it.
 */
export function isNormalQuery(query: Record<string, unknown>): boolean {
  const expected = toRouteQuery(parseListQuery(query))
  return ROUTE_KEYS.every(key => (key in query ? query[key] === expected[key] : !(key in expected)))
}

/**
 * The page to replace the URL's with when it is past the end (a bookmark
 * after deletions): no rows came back, yet something matches. Else null --
 * also when nothing matches at all (the empty state) or the page would not move.
 */
export function clampPage(
  state: DashboardListState,
  meta: { total: number; total_pages: number },
  rows: number,
): number | null {
  if (rows > 0 || meta.total <= 0 || state.page <= meta.total_pages) return null
  const last = Math.max(1, meta.total_pages)
  return last === state.page ? null : last
}

/** How long a typing burst lasts without a change. */
export const SEARCH_BURST_MS = 1500

/**
 * A debounced search change: the first of a typing burst adds a history
 * entry (push), later ones in the same burst replace it. `burstAt` is the
 * time of the burst's last change, null after a blur or a Back/Forward.
 */
export function searchHistoryMethod(burstAt: number | null, now: number): 'push' | 'replace' {
  return burstAt !== null && now - burstAt < SEARCH_BURST_MS ? 'replace' : 'push'
}

/**
 * The state after switching to `tab`, on page 1. To My: the Shared-only
 * filters (owners, the access chip, "needs attention") are dropped and
 * "Recently published" becomes "Recently edited"; to Shared: the status.
 */
export function switchTab(state: DashboardListState, tab: DashboardTab): DashboardListState {
  const next: DashboardListState = { ...state, tab, page: 1 }
  if (tab === 'my') {
    next.owners = []
    next.access = 'all'
    next.attention = false
    if (next.sort === 'published') next.sort = 'edited'
  } else {
    next.status = 'any'
  }
  return next
}

/** "Clear all": the search, the type, the access chip and every funnel filter. The tab and the sort stay. */
export function clearAllFilters(state: DashboardListState): DashboardListState {
  return { ...DEFAULT_STATE, agents: [], owners: [], tab: state.tab, sort: state.sort }
}

export type FilterChipKind = 'q' | 'agent' | 'owner' | 'created' | 'edited' | 'status' | 'starred' | 'attention'
export interface FilterChip {
  id: string
  kind: FilterChipKind
  value?: string
}

/** The active-filters row: one removable chip per value that applies on the state's tab. */
export function activeFilterChips(state: DashboardListState): FilterChip[] {
  const shared = state.tab === 'shared'
  const chips: FilterChip[] = []
  const q = state.q.trim()
  if (q) chips.push({ id: 'q', kind: 'q', value: q })
  for (const id of state.agents) chips.push({ id: `agent:${id}`, kind: 'agent', value: id })
  if (shared) for (const id of state.owners) chips.push({ id: `owner:${id}`, kind: 'owner', value: id })
  if (rangeActive(state.created, state.createdFrom, state.createdTo)) chips.push({ id: 'created', kind: 'created', value: state.created })
  if (rangeActive(state.edited, state.editedFrom, state.editedTo)) chips.push({ id: 'edited', kind: 'edited', value: state.edited })
  if (!shared && state.status !== 'any') chips.push({ id: 'status', kind: 'status', value: state.status })
  if (state.starred) chips.push({ id: 'starred', kind: 'starred' })
  if (state.attention) chips.push({ id: 'attention', kind: 'attention' })
  return chips
}

/** The state without one chip's value, on page 1. An unknown chip leaves the state as it is. */
export function removeFilterChip(state: DashboardListState, id: string): DashboardListState {
  const [kind, value] = id.includes(':') ? [id.slice(0, id.indexOf(':')), id.slice(id.indexOf(':') + 1)] : [id, '']
  const next = { ...state, page: 1 }
  switch (kind) {
    case 'q': return { ...next, q: '' }
    case 'agent': return { ...next, agents: state.agents.filter(a => a !== value) }
    case 'owner': return { ...next, owners: state.owners.filter(o => o !== value) }
    case 'created': return { ...next, created: 'any', createdFrom: '', createdTo: '' }
    case 'edited': return { ...next, edited: 'any', editedFrom: '', editedTo: '' }
    case 'status': return { ...next, status: 'any' }
    case 'starred': return { ...next, starred: false }
    case 'attention': return { ...next, attention: false }
    default: return state
  }
}

function rangeActive(preset: DatePreset, from: string, to: string): boolean {
  if (preset === 'custom') return !!(from || to)
  return preset !== 'any'
}

/** The funnel's badge: its filters that apply on the state's tab (each agent and owner counts). */
export function activeFilterCount(state: DashboardListState): number {
  const shared = state.tab === 'shared'
  return state.agents.length
    + (shared ? state.owners.length : 0)
    + Number(rangeActive(state.created, state.createdFrom, state.createdTo))
    + Number(rangeActive(state.edited, state.editedFrom, state.editedTo))
    + Number(!shared && state.status !== 'any')
    + Number(state.starred)
}

// --- what a card says --------------------------------------------------------------------

const REASON_ICON: Record<string, string> = {
  direct: 'i-heroicons-user',
  group: 'i-heroicons-user-group',
  project: 'i-heroicons-folder',
  org: 'i-heroicons-building-office',
  public: 'i-heroicons-globe-alt',
}
const OTHER_ICON = 'i-heroicons-share'

/** The primary reason's group or project name, or '' when the API gave none. */
function viaName(card: DashboardCardData): string {
  return (card.access?.via_name ?? '').trim()
}

function primaryReason(card: DashboardCardData): string | null {
  const reasons = card.access?.reasons ?? []
  return card.access?.primary || reasons[0] || null
}

/** `org` reads "Public link" when anyone with the link can open it. */
function shownReason(reason: string, card: DashboardCardData): string {
  return reason === 'org' && card.visibility === 'public' ? 'public' : reason
}

/**
 * The label on a Shared card: how it reached the reader, by its primary
 * reason only (whether they can edit is the pencil's to say). Null on My.
 */
export function accessLabel(
  card: DashboardCardData,
): { key: string; params: Record<string, string>; icon: string } | null {
  const primary = primaryReason(card)
  if (!primary) return null
  const reason = shownReason(primary, card)
  // A reason this page does not know yet reads "Shared", never a raw key.
  if (!REASON_ICON[reason]) return { key: 'dashboards.reason.other', params: {}, icon: OTHER_ICON }
  if (reason === 'group' || reason === 'project') {
    const name = viaName(card)
    return name
      ? { key: `dashboards.reason.${reason}`, params: { name }, icon: REASON_ICON[reason] }
      : { key: `dashboards.reason.${reason}Any`, params: {}, icon: REASON_ICON[reason] }
  }
  return { key: `dashboards.reason.${reason}`, params: {}, icon: REASON_ICON[reason] }
}

/**
 * The label's hover text, as parts: every reason, in the API's order, then
 * what the reader may do. `via_name` names the primary reason's group or
 * project only, so another group or project reason is told without a name.
 */
export function accessTitleParts(card: DashboardCardData): Array<{ key: string; params: Record<string, string> }> {
  const reasons = card.access?.reasons ?? []
  if (!reasons.length) return []
  const primary = primaryReason(card)
  const parts = reasons.map((r): { key: string; params: Record<string, string> } => {
    const reason = shownReason(r, card)
    if (reason === 'direct') {
      const owner = card.owner?.name
      return owner ? { key: 'dashboards.reasonTitle.direct', params: { owner } } : { key: 'dashboards.reason.direct', params: {} }
    }
    if (reason === 'group' || reason === 'project') {
      return r === primary && viaName(card)
        ? { key: `dashboards.reasonTitle.${reason}`, params: { name: viaName(card) } }
        : { key: `dashboards.reasonTitle.${reason}Other`, params: {} }
    }
    if (!REASON_ICON[reason]) return { key: 'dashboards.reason.other', params: {} }
    return { key: `dashboards.reasonTitle.${reason}`, params: {} }
  })
  const canEdit = card.role === 'owner' || card.role === 'edit'
  parts.push({ key: canEdit ? 'dashboards.reasonTitle.canEdit' : 'dashboards.reasonTitle.viewOnly', params: {} })
  return parts
}

export type CardMetaPart = 'by' | 'edited' | 'created' | 'published'

/**
 * The parts of a card's meta line, in order. My: Edited, Created. Shared:
 * by owner, then Edited -- or, reached only through the organization,
 * Published. A part whose value is missing is left out, never shown empty.
 * Edited falls back to the creation time.
 */
export function cardMeta(card: DashboardCardData, tab: DashboardTab): CardMetaPart[] {
  const has: Record<CardMetaPart, boolean> = {
    by: !!card.owner?.name,
    edited: !!(card.edited_at || card.created_at),
    created: !!card.created_at,
    published: !!card.published_at,
  }
  const parts: CardMetaPart[] = tab === 'my'
    ? ['edited', 'created']
    : ['by', card.access?.primary === 'org' ? 'published' : 'edited']
  return parts.filter(part => has[part])
}

/**
 * A refetched page of cards, keeping the local star of every card whose
 * star request is still in flight (`pending`): the response may predate it.
 * Every other card takes the server's. The response is not mutated.
 */
export function mergeStarState<T extends { id: string; is_starred?: boolean }>(
  fresh: readonly T[],
  pending: ReadonlySet<string>,
  local: readonly T[],
): T[] {
  const localStar = new Map(local.map(card => [card.id, card.is_starred]))
  return fresh.map(card => (pending.has(card.id) && localStar.has(card.id)
    ? { ...card, is_starred: localStar.get(card.id) }
    : card))
}

/**
 * The pill next to the mode pill. On Shared: "New", else "Unpublished
 * changes" for an editor. On My: a published dashboard's state ("Unpublished
 * changes", or where it is published). Changes need `unpublished_changes ===
 * true`: the API leaves it null for a view-only reader.
 */
export function statusPill(card: DashboardCardData, tab: DashboardTab): { key: string; tone: string } | null {
  const pill = (tone: string) => ({ key: `dashboards.pill.${tone}`, tone })
  if (tab === 'shared') {
    if (card.is_new) return pill('new')
    return card.unpublished_changes === true ? pill('changes') : null
  }
  if (!card.published) return null
  if (card.unpublished_changes === true) return pill('changes')
  if (card.visibility === 'internal') return pill('org')
  if (card.visibility === 'public') return pill('public')
  return pill('published')
}
