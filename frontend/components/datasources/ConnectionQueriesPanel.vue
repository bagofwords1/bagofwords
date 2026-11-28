<template>
  <div class="connection-queries min-w-0" data-testid="connection-queries">
    <!-- Nothing loaded yet: a spinner, or why it could not load. Never a
         made-up "0 / —" with empty lists that read as "no queries". -->
    <div v-if="!loaded && !loadError" class="flex justify-center py-10" data-testid="queries-loading">
      <Spinner class="h-5 w-5" />
    </div>
    <div v-else-if="!loaded" role="alert" class="flex flex-col items-center gap-2 rounded-lg border border-dashed border-line px-6 py-10 text-center" data-testid="queries-unavailable">
      <UIcon name="i-heroicons-exclamation-triangle" class="h-6 w-6 text-amber-500" aria-hidden="true" />
      <p class="text-sm font-medium text-ink">{{ $t('connectionQueries.unavailableTitle') }}</p>
      <p class="max-w-md text-xs text-ink-muted" data-testid="queries-unavailable-reason">{{ loadError }}</p>
      <UButton size="xs" color="gray" variant="soft" icon="i-heroicons-arrow-path" class="mt-1" :loading="refreshing" data-testid="queries-retry" @click="refreshAll">{{ $t('common.retry') }}</UButton>
    </div>
    <template v-else>
    <!-- Slot summary: how busy the connection is against its app-wide limit -->
    <div class="flex flex-wrap items-center gap-x-4 gap-y-2" data-testid="queries-summary">
      <div class="flex items-baseline gap-1.5">
        <span class="text-2xl font-semibold text-ink nums" data-testid="queries-running-count">{{ summary.running }}</span>
        <span class="text-sm text-ink-muted">/ {{ summary.limit ?? '—' }}</span>
        <span class="text-sm text-ink-2 ms-1">{{ $t('connectionQueries.runningNow') }}</span>
      </div>
      <span v-if="summary.queued" class="inline-flex items-center gap-1 rounded-md bg-amber-50 dark:bg-amber-950 px-2 py-0.5 text-xs text-amber-800 dark:text-amber-300" data-testid="queries-queued-count">
        {{ $t('connectionQueries.queuedCount', { n: summary.queued }) }}
      </span>
      <span class="text-xs text-ink-muted" data-testid="queries-limit-source">{{ limitSourceLabel }}</span>
      <div class="ms-auto flex items-center gap-2">
        <span class="inline-flex items-center gap-1.5 text-2xs text-ink-muted" :title="$t('connectionQueries.liveHint')">
          <span class="h-1.5 w-1.5 rounded-full" :class="loadError ? 'bg-red-500' : 'bg-green-500'" />{{ $t('connectionQueries.live') }}
        </span>
        <UButton size="xs" color="gray" variant="ghost" icon="i-heroicons-arrow-path" :loading="refreshing" :aria-label="$t('connectionQueries.refresh')" @click="refreshAll" />
      </div>
    </div>
    <p v-if="loadError" role="alert" class="mt-2 text-xs text-red-600">{{ loadError }}</p>

    <!-- Running and queued queries -->
    <section class="mt-5" aria-labelledby="queries-running-heading">
      <h4 id="queries-running-heading" class="text-sm font-medium text-ink">{{ $t('connectionQueries.runningTitle') }}</h4>
      <div v-if="!running.length" class="mt-2 rounded-lg border border-dashed border-line px-4 py-5 text-center text-sm text-ink-muted" data-testid="queries-running-empty">
        {{ $t('connectionQueries.noneRunning') }}
      </div>
      <ul v-else class="mt-2 divide-y divide-line rounded-lg border border-line" data-testid="queries-running-list">
        <li v-for="q in running" :key="q.id" class="px-3 py-2.5" :data-testid="`running-query-${q.id}`">
          <div class="flex items-start gap-3">
            <StatusChip :status="q.status" class="mt-0.5" />
            <div class="min-w-0 flex-1">
              <button type="button" class="block w-full text-start" :aria-expanded="expanded === q.id" @click="toggle(q.id)">
                <code class="block truncate font-mono text-xs text-ink" dir="ltr">{{ oneLine(q.query_text) }}</code>
              </button>
              <div class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-ink-muted">
                <span>{{ whoLabel(q) }}</span>
                <span aria-hidden="true">·</span>
                <span>{{ sourceLabel(q.source) }}</span>
                <template v-if="q.agent?.name"><span aria-hidden="true">·</span><span>{{ q.agent.name }}</span></template>
                <span aria-hidden="true">·</span>
                <span class="nums" data-testid="query-elapsed">{{ q.status === 'queued' ? $t('connectionQueries.waitingFor', { time: duration(liveMs(q, 'queued')) }) : $t('connectionQueries.runningFor', { time: duration(liveMs(q, 'running')) }) }}</span>
              </div>
            </div>
            <div class="shrink-0">
              <UTooltip v-if="q.killable === false && q.status !== 'queued'" :text="$t('connectionQueries.notKillableHint')">
                <span class="text-xs text-ink-muted" data-testid="query-not-killable">{{ $t('connectionQueries.notKillable') }}</span>
              </UTooltip>
              <span v-else-if="q.kill_requested_at" class="inline-flex items-center gap-1.5 text-xs text-ink-muted" data-testid="query-stopping">
                <Spinner class="h-3 w-3" />{{ $t('connectionQueries.stopping') }}
              </span>
              <template v-else-if="confirming === q.id">
                <span class="me-2 text-xs text-ink-2">{{ $t('connectionQueries.killConfirm') }}</span>
                <UButton size="xs" color="red" :loading="killing === q.id" data-testid="query-kill-confirm" @click="kill(q)">{{ $t('connectionQueries.kill') }}</UButton>
                <UButton size="xs" color="gray" variant="ghost" class="ms-1" @click="confirming = null">{{ $t('common.cancel') }}</UButton>
              </template>
              <UButton v-else size="xs" color="red" variant="soft" icon="i-heroicons-stop-circle" data-testid="query-kill" @click="confirming = q.id">{{ $t('connectionQueries.kill') }}</UButton>
            </div>
          </div>
          <QueryDetail v-if="expanded === q.id" :query="q" class="mt-2" />
        </li>
      </ul>
      <div v-if="runningHasMore" class="mt-2 flex justify-center">
        <UButton size="xs" color="gray" variant="ghost" data-testid="running-load-more" @click="loadMoreRunning">{{ $t('connectionQueries.loadMore') }}</UButton>
      </div>
      <p v-if="killError" role="alert" class="mt-2 text-xs text-red-600">{{ killError }}</p>
    </section>

    <!-- History -->
    <section class="mt-6" aria-labelledby="queries-history-heading">
      <div class="flex flex-wrap items-center gap-2">
        <h4 id="queries-history-heading" class="text-sm font-medium text-ink me-auto">{{ $t('connectionQueries.historyTitle') }}</h4>
        <USelectMenu v-model="statusFilter" :options="statusOptions" value-attribute="value" option-attribute="label" size="xs" class="w-40" data-testid="history-status-filter" />
        <UInput v-model="search" size="xs" icon="i-heroicons-magnifying-glass" :placeholder="$t('connectionQueries.searchPlaceholder')" class="w-52" data-testid="history-search" />
      </div>
      <div v-if="historyLoading && !history.length" class="mt-4 flex justify-center"><Spinner class="h-5 w-5" /></div>
      <div v-else-if="!history.length && historyLoaded" class="mt-2 rounded-lg border border-dashed border-line px-4 py-5 text-center text-sm text-ink-muted" data-testid="queries-history-empty">
        {{ hasFilters ? $t('connectionQueries.noMatches') : $t('connectionQueries.noHistory') }}
      </div>
      <div v-else class="mt-2 overflow-x-auto rounded-lg border border-line">
        <table class="w-full text-xs" data-testid="queries-history-table">
          <thead class="bg-surface-subtle text-ink-muted">
            <tr>
              <th class="px-3 py-2 text-start font-medium">{{ $t('connectionQueries.colStatus') }}</th>
              <th class="px-3 py-2 text-start font-medium">{{ $t('connectionQueries.colQuery') }}</th>
              <th class="px-3 py-2 text-start font-medium">{{ $t('connectionQueries.colWho') }}</th>
              <th class="px-3 py-2 text-end font-medium">{{ $t('connectionQueries.colDuration') }}</th>
              <th class="px-3 py-2 text-end font-medium">{{ $t('connectionQueries.colWait') }}</th>
              <th class="px-3 py-2 text-end font-medium">{{ $t('connectionQueries.colRows') }}</th>
              <th class="px-3 py-2 text-end font-medium">{{ $t('connectionQueries.colWhen') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-line">
            <template v-for="q in history" :key="q.id">
              <tr class="cursor-pointer hover:bg-surface-subtle" :data-testid="`history-row-${q.id}`" :aria-expanded="expanded === q.id" @click="toggle(q.id)">
                <td class="px-3 py-2 align-top"><StatusChip :status="q.status" /></td>
                <td class="max-w-[22rem] px-3 py-2 align-top">
                  <code class="block truncate font-mono text-ink" dir="ltr">{{ oneLine(q.query_text) }}</code>
                  <span v-if="reason(q) && expanded !== q.id" class="mt-0.5 block truncate text-2xs text-red-600">{{ oneLine(reason(q)) }}</span>
                </td>
                <td class="px-3 py-2 align-top text-ink-2">
                  <span class="block truncate max-w-[10rem]">{{ whoLabel(q) }}</span>
                  <span class="block text-2xs text-ink-muted">{{ sourceLabel(q.source) }}</span>
                </td>
                <td class="px-3 py-2 align-top text-end nums text-ink-2">{{ q.duration_ms != null ? duration(q.duration_ms) : '—' }}</td>
                <td class="px-3 py-2 align-top text-end nums text-ink-muted">{{ q.queued_ms != null && q.queued_ms >= 50 ? duration(q.queued_ms) : '—' }}</td>
                <td class="px-3 py-2 align-top text-end nums text-ink-2">{{ q.rows ?? '—' }}</td>
                <td class="px-3 py-2 align-top text-end text-ink-muted whitespace-nowrap" :title="absolute(q.queued_at)">{{ relativeTime(q.queued_at) }}</td>
              </tr>
              <tr v-if="expanded === q.id">
                <td colspan="7" class="bg-surface-subtle px-3 pb-3 pt-1"><QueryDetail :query="q" /></td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <div v-if="historyHasMore" class="mt-2 flex justify-center">
        <UButton size="xs" color="gray" variant="ghost" :loading="historyLoading" data-testid="history-load-more" @click="loadMore">{{ $t('connectionQueries.loadMore') }}</UButton>
      </div>
      <p class="mt-3 text-2xs text-ink-muted">{{ $t('connectionQueries.retentionNote') }}</p>
    </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'

interface QueryItem {
  id: string
  status: string
  policy_version?: string | null
  protected_statement?: string | null
  query_text: string | null
  query_truncated: boolean
  source: string | null
  client_type: string | null
  user: { id: string; name?: string; email?: string } | null
  agent: { id: string; name: string } | null
  report: { id: string; title: string } | null
  report_id: string | null
  tool_attempt: number | null
  code_attempt: number | null
  seq: number | null
  queued_at: string | null
  started_at: string | null
  ended_at: string | null
  queued_ms: number | null
  duration_ms: number | null
  rows: number | null
  error: string | null
  error_code: string | null
  cancel_outcome: string | null
  kill_requested_at: string | null
  killed_by: { id: string; name?: string; email?: string } | null
  worker_id: string | null
}

const props = defineProps<{ connectionId: string; active?: boolean }>()
const { t } = useI18n()
const { relativeTime } = useRelativeTime()
const { formatDateTime, toDate } = useFormatDate()
const { getErrorMessage, getErrorCode } = useErrorMessage()

const summary = ref<{ running: number; queued: number; limit: number | null; limit_source?: string }>({ running: 0, queued: 0, limit: null })
const running = ref<QueryItem[]>([])
// The live list is paged like history: a burst bigger than one page stays
// reachable (and killable). Polling refreshes every row shown so far.
const RUNNING_PAGE = 50
const RUNNING_FETCH_MAX = 200
const runningShown = ref(RUNNING_PAGE)
const runningHasMore = ref(false)
const history = ref<QueryItem[]>([])
const historyTotal = ref(0)
const historyHasMore = ref(false)
const historyLoading = ref(false)
const refreshing = ref(false)
const loadError = ref('')
// True once the live list has loaded at least once. Until then the tab shows a
// spinner or the reason it could not load, never an empty-looking summary.
const loaded = ref(false)
const historyLoaded = ref(false)
const killError = ref('')
const expanded = ref<string | null>(null)
const confirming = ref<string | null>(null)
const killing = ref<string | null>(null)
const statusFilter = ref('all')
const search = ref('')
const now = ref(Date.now())
const PAGE = 25

const statusOptions = computed(() => [
  { value: 'all', label: t('connectionQueries.filterAll') },
  { value: 'success', label: t('connectionQueries.status.success') },
  { value: 'error', label: t('connectionQueries.status.error') },
  { value: 'timeout', label: t('connectionQueries.status.timeout') },
  { value: 'busy', label: t('connectionQueries.status.busy') },
  { value: 'cancelled', label: t('connectionQueries.status.cancelled') },
  { value: 'stale', label: t('connectionQueries.status.stale') },
])
const hasFilters = computed(() => statusFilter.value !== 'all' || !!search.value.trim())
const limitSourceLabel = computed(() => {
  const src = summary.value.limit_source
  if (src === 'connection') return t('connectionQueries.limitFromConnection')
  if (src === 'organization') return t('connectionQueries.limitFromOrg')
  return t('connectionQueries.limitDefault')
})

function oneLine(text: string | null | undefined): string {
  return (text || '').replace(/\s+/g, ' ').trim()
}
function duration(ms: number | null | undefined): string {
  if (ms == null) return '—'
  if (ms < 1000) return t('connectionQueries.ms', { n: Math.round(ms) })
  const s = ms / 1000
  if (s < 60) return t('connectionQueries.seconds', { n: s < 10 ? s.toFixed(1) : Math.round(s) })
  const m = Math.floor(s / 60)
  return t('connectionQueries.minutes', { m, s: Math.round(s - m * 60) })
}
function liveMs(q: QueryItem, phase: 'queued' | 'running'): number | null {
  const start = toDate(phase === 'queued' ? q.queued_at : (q.started_at || q.queued_at))
  if (!start) return phase === 'queued' ? q.queued_ms : q.duration_ms
  return Math.max(0, now.value - start.getTime())
}
function absolute(iso: string | null): string {
  return iso ? formatDateTime(iso) : ''
}
function whoLabel(q: QueryItem): string {
  return q.user?.name || q.user?.email || t('connectionQueries.system')
}
function sourceLabel(source: string | null): string {
  const s = source || ''
  if (s === 'agent') return t('connectionQueries.source.chat')
  if (s === 'agent_eval') return t('connectionQueries.source.eval')
  if (s === 'query_run' || s === 'query_preview') return t('connectionQueries.source.query')
  if (s === 'entity_run') return t('connectionQueries.source.entity')
  if (s.startsWith('dashboard')) return t('connectionQueries.source.dashboard')
  if (s === 'viewer_refresh') return t('connectionQueries.source.viewer')
  if (s.startsWith('scheduled')) return t('connectionQueries.source.scheduled')
  if (!s || s === 'system') return t('connectionQueries.source.system')
  return s
}
// BOW's own outcomes in the viewer's language; source errors verbatim.
function reason(q: QueryItem): string {
  if (q.error_code) return t(`connectionQueries.reason.${q.error_code}`)
  return q.error || ''
}
function toggle(id: string) {
  expanded.value = expanded.value === id ? null : id
}

async function fetchRunning() {
  const items: QueryItem[] = []
  let body: any = {}
  let offset = 0
  while (offset < runningShown.value) {
    const limit = Math.min(RUNNING_FETCH_MAX, runningShown.value - offset)
    const { data, error } = await useMyFetch(`/connections/${props.connectionId}/queries`, {
      method: 'GET', query: { scope: 'running', limit, offset },
    })
    if (error.value) {
      loadError.value = loadFailure(error.value)
      return
    }
    body = data.value || {}
    items.push(...(body.items || []))
    offset += limit
    if (!body.has_more) break
  }
  loadError.value = ''
  loaded.value = true
  summary.value = body.summary || summary.value
  running.value = items
  runningHasMore.value = !!body.has_more
  if (confirming.value && !running.value.some(q => q.id === confirming.value)) confirming.value = null
}

// A 404 that is not one of our typed errors means the route itself is missing:
// the server predates the Queries view (frontend and backend out of step).
function loadFailure(err: any): string {
  const status = err?.statusCode ?? err?.status ?? err?.response?.status
  if (status === 404 && !getErrorCode(err)) return t('connectionQueries.unavailableOutdated')
  return getErrorMessage(err, t('connectionQueries.loadFailed'))
}

function loadMoreRunning() {
  runningShown.value += RUNNING_PAGE
  fetchRunning()
}

async function fetchHistory(reset = true) {
  historyLoading.value = true
  try {
    const offset = reset ? 0 : history.value.length
    const query: Record<string, any> = { limit: reset ? Math.max(PAGE, history.value.length || 0) : PAGE, offset }
    if (statusFilter.value && statusFilter.value !== 'all') query.status = statusFilter.value
    if (search.value.trim()) query.search = search.value.trim()
    const { data, error } = await useMyFetch(`/connections/${props.connectionId}/queries`, { method: 'GET', query })
    if (error.value) {
      loadError.value = loadFailure(error.value)
      return
    }
    const body: any = data.value || {}
    historyLoaded.value = true
    history.value = reset ? (body.items || []) : [...history.value, ...(body.items || [])]
    historyTotal.value = body.total || 0
    historyHasMore.value = !!body.has_more
  } finally {
    historyLoading.value = false
  }
}

async function refreshAll() {
  refreshing.value = true
  try {
    await Promise.all([fetchRunning(), fetchHistory(true)])
  } finally {
    refreshing.value = false
  }
}

function loadMore() {
  fetchHistory(false)
}

async function kill(q: QueryItem) {
  killing.value = q.id
  killError.value = ''
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connectionId}/queries/${q.id}/kill`, { method: 'POST' })
    if (error.value) {
      killError.value = getErrorMessage(error.value, t('connectionQueries.killFailed'))
    } else {
      const updated: any = data.value
      running.value = running.value.map(r => (r.id === q.id ? { ...r, ...updated } : r))
    }
  } finally {
    killing.value = null
    confirming.value = null
    await fetchRunning()
  }
}

// Live: running every 2s, history every 6s (only the first page, so a manager
// reading older pages is not reset under them), elapsed time every second.
let runningTimer: ReturnType<typeof setInterval> | null = null
let historyTimer: ReturnType<typeof setInterval> | null = null
let clockTimer: ReturnType<typeof setInterval> | null = null
let lastRunningIds = ''
function start() {
  stop()
  refreshAll()
  runningTimer = setInterval(async () => {
    await fetchRunning()
    const ids = running.value.map(q => q.id).join(',')
    if (ids !== lastRunningIds) {
      lastRunningIds = ids
      if (history.value.length <= PAGE) fetchHistory(true)
    }
  }, 2000)
  historyTimer = setInterval(() => {
    if (history.value.length <= PAGE && !historyLoading.value) fetchHistory(true)
  }, 6000)
  clockTimer = setInterval(() => { now.value = Date.now() }, 1000)
}
function stop() {
  for (const timer of [runningTimer, historyTimer, clockTimer]) if (timer) clearInterval(timer)
  runningTimer = historyTimer = clockTimer = null
}

let searchDebounce: ReturnType<typeof setTimeout> | null = null
watch(statusFilter, () => fetchHistory(true))
watch(search, () => {
  if (searchDebounce) clearTimeout(searchDebounce)
  searchDebounce = setTimeout(() => fetchHistory(true), 300)
})
watch(() => [props.connectionId, props.active], () => {
  if (props.active === false) stop()
  else start()
})
onMounted(() => { if (props.active !== false) start() })
onBeforeUnmount(stop)

// ── Small presentational pieces ─────────────────────────────────────────────
const STATUS_STYLES: Record<string, string> = {
  running: 'bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300',
  queued: 'bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  success: 'bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-300',
  error: 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300',
  timeout: 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300',
  busy: 'bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  cancelled: 'bg-surface-muted text-ink-2',
  stale: 'bg-surface-muted text-ink-muted',
}
const StatusChip = defineComponent({
  props: { status: { type: String, required: true } },
  setup(p) {
    return () => h('span', {
      class: ['inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-2xs font-medium', STATUS_STYLES[p.status] || STATUS_STYLES.stale],
      'data-status': p.status,
    }, [
      p.status === 'running' ? h('span', { class: 'h-1.5 w-1.5 rounded-full bg-blue-500 motion-safe:animate-pulse' }) : null,
      t(`connectionQueries.status.${p.status}`),
    ])
  },
})

const QueryDetail = defineComponent({
  props: { query: { type: Object as () => QueryItem, required: true } },
  setup(p) {
    const copied = ref(false)
    async function copy() {
      try {
        await navigator.clipboard.writeText(p.query.query_text || '')
        copied.value = true
        setTimeout(() => { copied.value = false }, 1500)
      } catch { /* clipboard unavailable */ }
    }
    return () => {
      const q = p.query
      const facts: Array<[string, string]> = []
      if (q.agent?.name) facts.push([t('connectionQueries.agent'), q.agent.name])
      facts.push([t('connectionQueries.source.label'), sourceLabel(q.source)])
      if (q.code_attempt || q.tool_attempt) {
        facts.push([t('connectionQueries.attempt'), t('connectionQueries.attemptValue', { code: q.code_attempt ?? 1, tool: q.tool_attempt ?? 1, seq: (q.seq ?? 0) + 1 })])
      }
      if (q.started_at) facts.push([t('connectionQueries.started'), absolute(q.started_at)])
      if (q.ended_at) facts.push([t('connectionQueries.ended'), absolute(q.ended_at)])
      if (q.killed_by) facts.push([t('connectionQueries.killedBy'), q.killed_by.name || q.killed_by.email || ''])
      if (q.cancel_outcome) facts.push([t('connectionQueries.sourceCancel'), q.cancel_outcome])
      return h('div', { class: 'space-y-2', 'data-testid': 'query-detail' }, [
        h('div', { class: 'relative' }, [
          q.policy_version ? h('span', {class:'font-medium'}, t('liveAccess.submitted')) : null,
          h('pre', { class: 'max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-muted p-3 pe-16 font-mono text-xs text-ink', dir: 'ltr', 'data-testid': 'query-full-text' }, q.query_text || ''),
          q.policy_version ? h('div', {class:'space-y-1'}, [
            h('strong', t(q.protected_statement ? 'liveAccess.sent' : 'liveAccess.blocked')),
            q.protected_statement ? h('pre', {class:'overflow-x-auto whitespace-pre-wrap'}, q.protected_statement) : null,
            h('code', q.policy_version)
          ]) : null,
          h('button', { type: 'button', class: 'absolute end-2 top-2 rounded border border-line bg-surface px-2 py-0.5 text-2xs text-ink-2 hover:text-ink', onClick: (e: Event) => { e.stopPropagation(); copy() } },
            copied.value ? t('connectionQueries.copied') : t('connectionQueries.copy')),
        ]),
        q.query_truncated ? h('p', { class: 'text-2xs text-ink-muted' }, t('connectionQueries.truncated')) : null,
        reason(q) ? h('p', { class: 'whitespace-pre-wrap break-words rounded-md bg-red-50 dark:bg-red-950 px-3 py-2 text-xs text-red-700 dark:text-red-300', 'data-testid': 'query-error' }, reason(q)) : null,
        h('dl', { class: 'grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-2xs' }, facts.flatMap(([k, v]) => [
          h('dt', { class: 'text-ink-muted' }, k),
          h('dd', { class: 'text-ink-2 break-words' }, v),
        ])),
        q.report_id ? h('a', { href: `/reports/${q.report_id}`, target: '_blank', rel: 'noopener', class: 'inline-flex items-center gap-1 text-xs text-blue-600 hover:underline', 'data-testid': 'query-open-report', onClick: (e: Event) => e.stopPropagation() },
          q.report?.title ? t('connectionQueries.openReportNamed', { title: q.report.title }) : t('connectionQueries.openReport')) : null,
      ])
    }
  },
})
</script>
