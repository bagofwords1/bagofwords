// Shared presentation rules for the notification inbox (NotificationModal.vue)
// and the Review feed (ReviewFeed.vue): icons, source filters, per-type
// actions/links and locale-aware title/body rendering.
//
// The icon maps are also read by a backend guardrail test
// (backend/tests/unit/test_notification_catalog.py): every notification type
// the backend emits must have an entry in TYPE_ICONS and every source one in
// SOURCE_ICONS. Keep each map a flat `key: 'icon'` object literal.

export interface NotificationLike {
  source?: string
  type: string
  title: string
  body?: string | null
  link?: string | null
  subject?: Record<string, any>
}

// Icon by type, falling back to source.
export const TYPE_ICONS: Record<string, string> = {
  low_confidence: 'i-heroicons-arrow-trending-down',
  schema_changed: 'i-heroicons-table-cells',
  slow_query: 'i-heroicons-clock',
  query_error: 'i-heroicons-exclamation-triangle',
  instruction_suggestion: 'i-heroicons-sparkles',
  share_conversation: 'i-heroicons-chat-bubble-left-right',
  share_artifact: 'i-heroicons-chart-bar-square',
  agent_access: 'i-heroicons-user-plus',
  access_request: 'i-heroicons-lock-open',
  access_granted: 'i-heroicons-check-badge',
  scheduled_run: 'i-heroicons-clock',
  scheduled_run_failed: 'i-heroicons-exclamation-triangle',
  trigger_run: 'i-heroicons-bolt',
  automation_failed: 'i-heroicons-exclamation-triangle',
  checkin_followup: 'i-heroicons-arrow-path-rounded-square',
  nightly_learning: 'i-heroicons-moon',
  connection_down: 'i-heroicons-signal-slash',
  connection_restored: 'i-heroicons-signal',
  delivery_skipped: 'i-heroicons-envelope',
  prompt_run_for: 'i-heroicons-play-circle',
  notify: 'i-heroicons-sparkles',
}

export const SOURCE_ICONS: Record<string, string> = {
  review: 'i-heroicons-bell-alert',
  share: 'i-heroicons-share',
  schedule: 'i-heroicons-clock',
  trigger: 'i-heroicons-bolt',
  report_tool: 'i-heroicons-sparkles',
  checkin: 'i-heroicons-arrow-path-rounded-square',
  connection: 'i-heroicons-circle-stack',
}

export function iconFor(n: NotificationLike): string {
  return TYPE_ICONS[n.type] || SOURCE_ICONS[n.source || ''] || 'i-heroicons-bell'
}

// Inbox filter tabs. `sources: null` = no source filter.
export const NOTIFICATION_FILTERS = [
  { value: 'all', labelKey: 'notifications.filterAll', sources: null },
  { value: 'unread', labelKey: 'notifications.filterUnread', sources: null },
  { value: 'agents', labelKey: 'notifications.filterAgents', sources: ['review'] },
  { value: 'shares', labelKey: 'notifications.filterShares', sources: ['share'] },
  { value: 'runs', labelKey: 'notifications.filterRuns', sources: ['schedule', 'trigger', 'report_tool', 'checkin'] },
  { value: 'connections', labelKey: 'notifications.filterConnections', sources: ['connection'] },
] as const
export type NotificationFilter = typeof NOTIFICATION_FILTERS[number]['value']

// ── Review notifications: where to go ──────────────────────────────────────
// Mirrors backend review_service.review_link_for. Recomputed here so rows
// stored before that change (link = the generic /agents/<id>) also land on
// the right page.
function reviewFeedLink(s: Record<string, any>): string | null {
  if (!s.review_item_id) return null
  return s.data_source_id ? `/agents/${s.data_source_id}/review/${s.review_item_id}` : `/agents/review/${s.review_item_id}`
}

export function reviewLinkFor(n: NotificationLike): string | null {
  const s = n.subject || {}
  const ds = s.data_source_id
  const type = s.review_type || n.type
  if (type === 'instruction_suggestion' && s.instruction_id) return `/agents/instructions/${s.instruction_id}`
  if (ds && type === 'schema_changed') return `/agents/${ds}/tables`
  if (ds && type === 'slow_query') return `/agents/${ds}/queries`
  if (ds && type === 'low_confidence') return `/agents/${ds}/evals`
  return s.review_link || reviewFeedLink(s) || n.link || null
}

export function isReview(n: NotificationLike): boolean {
  return n.source === 'review'
}

export function linkFor(n: NotificationLike): string | null {
  return isReview(n) ? reviewLinkFor(n) : (n.link || null)
}

export interface NotificationAction {
  key: string
  labelKey: string
  to: string
  primary?: boolean
  // Leading icon: says where the button goes before the label is read.
  icon?: string
}

// Buttons in an expanded review row: the place to act on it first, then the
// item itself in the Review feed (where run-eval / run-training live).
export function actionsFor(n: NotificationLike): NotificationAction[] {
  if (!isReview(n)) return n.link ? [{ key: 'open', labelKey: 'notifications.detail.open', to: n.link, primary: true }] : []
  const s = n.subject || {}
  const ds = s.data_source_id
  const type = s.review_type || n.type
  const out: NotificationAction[] = []
  const primary = reviewLinkFor(n)
  const feed = s.review_link || reviewFeedLink(s)
  const primaryLabel: Record<string, string> = {
    schema_changed: 'notifications.detail.reviewTables',
    instruction_suggestion: 'notifications.detail.reviewInstruction',
    slow_query: 'notifications.detail.viewQueries',
    low_confidence: 'notifications.detail.openEvals',
  }
  if (primary && primary !== feed) {
    out.push({ key: 'primary', labelKey: primaryLabel[type] || 'notifications.detail.open', to: primary, primary: true })
  }
  // A schema change also started the agent's reliability check (evals, then
  // training if needed); its runs are listed on the agent's evals view.
  if (type === 'schema_changed' && ds && s.reliability_check) {
    out.push({ key: 'checks', labelKey: 'notifications.detail.viewChecks', to: `/agents/${ds}/evals`, icon: 'i-heroicons-beaker' })
  }
  if (feed) out.push({ key: 'review', labelKey: 'notifications.detail.openInReview', to: feed, primary: !out.length, icon: 'i-heroicons-inbox-stack' })
  return out
}

// ── Locale-aware text ───────────────────────────────────────────────────────
// Title/body are stored in English at emit time (the backend can't know which
// locale each future reader will use). Emit sites attach their interpolation
// params under subject.i18n, so known types re-render from the catalogue in
// the viewer's locale; rows without params (pre-existing, free-form tool
// output, user-authored copy) fall back to the stored text.
type T = (key: string, params?: Record<string, any>) => string
type TE = (key: string) => boolean

// Names interpolated into a sentence (agent, connection, report, person) are
// wrapped in Unicode first-strong isolates (FSI … PDI) so an English name with
// punctuation, e.g. "Warehouse (Postgres)", keeps its own direction inside a
// Hebrew/Arabic sentence instead of scrambling the brackets around it.
const FSI = '\u2068'
const PDI = '\u2069'
function i18nParams(n: NotificationLike): Record<string, any> | null {
  const p = (n.subject as any)?.i18n
  if (!p || typeof p !== 'object') return null
  const out: Record<string, any> = {}
  for (const [k, v] of Object.entries(p)) out[k] = typeof v === 'string' && v ? FSI + v + PDI : v
  return out
}

export function displayTitle(n: NotificationLike, t: T, te: TE): string {
  const p = i18nParams(n)
  if (p) {
    // A `title_variant` picks an alternate title (e.g. one naming the agent);
    // with no catalogue entry it falls back to the default title key.
    const variant = (n.subject as any)?.i18n?.title_variant
    const keys = variant
      ? [`notifications.content.${n.type}.title_${variant}`, `notifications.content.${n.type}.title`]
      : [`notifications.content.${n.type}.title`]
    for (const key of keys) if (te(key)) return t(key, p)
  }
  return n.title
}

export function displayBody(n: NotificationLike, t: T, te: TE): string | null {
  const p = i18nParams(n)
  if (p) {
    // A `variant` selects an alternate body template (e.g. body_no_actor); a
    // variant with no catalogue entry deliberately falls through to the stored
    // body (dynamic copy like a custom summary stays as emitted).
    const variant = (n.subject as any)?.i18n?.variant
    const key = variant
      ? `notifications.content.${n.type}.body_${variant}`
      : `notifications.content.${n.type}.body`
    if (te(key)) return t(key, p)
  }
  return n.body ?? null
}

// ── "Handled" reason ────────────────────────────────────────────────────────
// A review item closes for the whole team in one of a few ways; every one has
// a person behind it, directly ("marked") or through something they did.
// Returns e.g. "Dana marked it as handled" — or null when nothing is recorded.
export interface HandledInfo { how?: string | null; by_name?: string | null; at?: string | null }

export function handledText(h: HandledInfo | null | undefined, t: T): string | null {
  if (!h || !h.how) return null
  const name = h.by_name ? FSI + h.by_name + PDI : t('notifications.handled.someone')
  const key = `notifications.handled.${h.how}`
  const known = ['marked', 'suggestion_reviewed', 'suggestion_rejected', 'suggestion_voided',
    'training_passed', 'eval_passed', 'condition_cleared']
  return known.includes(h.how) ? t(key, { name }) : null
}

// ── Training-mode prompt ────────────────────────────────────────────────────
// "Improve in training mode" opens a new training conversation on the agent
// with this prompt prefilled (never auto-sent). It is built from what the item
// knows: the schema diff, or the examples GET /api/review/{id}/training-context
// returns (low-scored questions, slow or failed queries). With no examples it
// falls back to a generic brief for the type.
export interface TrainingExample { question?: string; title?: string; seconds?: number; error?: string; score?: number }
export interface TrainingPromptInput {
  type: string
  subject?: Record<string, any> | null
  groupCount?: number
  examples?: TrainingExample[]
}

const quote = (s: string) => `"${s}"`
const MAX_PROMPT_LINES = 20

function schemaLines(diff: any, t: T): string[] {
  const lines: string[] = []
  for (const name of diff?.added_tables || []) lines.push(t('reviewFeed.train.tableAdded', { table: name }))
  for (const name of diff?.removed_tables || []) lines.push(t('reviewFeed.train.tableRemoved', { table: name }))
  for (const ch of diff?.changed || []) {
    const parts: string[] = []
    const added = (ch.added_cols || []).map((c: any) => (typeof c === 'string' ? c : c?.name)).filter(Boolean)
    const removed = (ch.removed_cols || []).map((c: any) => (typeof c === 'string' ? c : c?.name)).filter(Boolean)
    if (added.length) parts.push(t('reviewFeed.train.colsAdded', { cols: added.join(', ') }))
    if (removed.length) parts.push(t('reviewFeed.train.colsRemoved', { cols: removed.join(', ') }))
    for (const tc of ch.type_changes || []) parts.push(t('reviewFeed.train.typeChanged', { col: tc.col, from: tc.from, to: tc.to }))
    if (parts.length) lines.push(`- ${ch.table}: ${parts.join('; ')}`)
  }
  return lines
}

export function trainingPrompt(input: TrainingPromptInput, t: T): string {
  const s = input.subject || {}
  const ex = input.examples || []
  const bullets = (fn: (e: TrainingExample) => string) => ex.map(e => `- ${fn(e)}`).join('\n')
  switch (input.type) {
    case 'schema_changed': {
      const all = schemaLines(s.diff, t)
      // The prompt travels in the URL (?prompt=), so a huge diff is listed in
      // part; the agent can read the rest from the tables themselves.
      const lines = all.length > MAX_PROMPT_LINES
        ? [...all.slice(0, MAX_PROMPT_LINES), t('reviewFeed.train.more', { n: all.length - MAX_PROMPT_LINES })]
        : all
      const connection = s.connection?.name || ''
      // Items from before the diff was recorded have neither changes nor a name.
      if (!connection) return t('reviewFeed.train.schemaUnnamed')
      if (!lines.length) return t('reviewFeed.train.schemaGeneric', { connection })
      return t('reviewFeed.train.schema', { connection, changes: lines.join('\n') })
    }
    case 'low_confidence':
      if (!ex.length) return t('reviewFeed.train.lowConfidenceGeneric', { n: input.groupCount || 1 })
      return t('reviewFeed.train.lowConfidence', { examples: bullets(e => quote(e.question || '')) })
    case 'slow_query':
      if (!ex.length) return t('reviewFeed.train.slowGeneric')
      return t('reviewFeed.train.slow', {
        examples: bullets(e => t('reviewFeed.train.slowLine', { q: quote(e.question || e.title || ''), s: e.seconds ?? '' })),
      })
    case 'query_error':
      if (!ex.length) return t('reviewFeed.train.errorGeneric')
      return t('reviewFeed.train.error', {
        examples: bullets(e => (e.error ? t('reviewFeed.train.errorLine', { q: quote(e.question || e.title || ''), error: e.error }) : quote(e.question || ''))),
      })
    default:
      return t('reviewFeed.train.generic')
  }
}
