<template>
    <div class="space-y-2" :class="{ 'compact-progress': compact }">
        <!-- Running / pending state -->
        <template v-if="isActive">
            <div class="text-[11px] text-gray-500">{{ $t('data.setupCurrentStage') }}</div>
            <div class="flex items-start justify-between gap-3 text-xs text-gray-700 dark:text-gray-300">
                <span class="font-medium break-words min-w-0">{{ summary }}</span>
                <div class="flex items-center gap-2 flex-none">
                    <span v-if="hasTotal">{{ percent }}%</span>
                    <button
                        v-if="allowCancel"
                        type="button"
                        class="inline-flex items-center gap-0.5 text-red-600 hover:text-red-700 disabled:opacity-50"
                        :disabled="cancelling"
                        @click="$emit('cancel')"
                    >
                        <UIcon name="heroicons-stop-circle" class="w-3.5 h-3.5" />
                        {{ cancelling ? $t('data.stopping') : $t('data.stop') }}
                    </button>
                </div>
            </div>
            <div :key="indexing?.phase" role="progressbar" :aria-label="$t('data.setupCurrentStage')" :aria-valuenow="hasTotal ? percent : undefined" :aria-valuemin="0" :aria-valuemax="100" class="h-1.5 w-full bg-gray-100 dark:bg-gray-800 rounded overflow-hidden">
                <div
                    class="h-full bg-gray-700 dark:bg-gray-300 transition-all duration-300"
                    :class="{ 'animate-pulse w-1/3': !hasTotal }"
                    :style="hasTotal ? { width: percent + '%' } : {}"
                ></div>
            </div>
        </template>

        <!-- Completed -->
        <div v-else-if="indexing?.status === 'completed'" :class="compact ? 'status-body text-gray-800 dark:text-gray-200 flex items-center gap-2' : 'text-xs text-green-700 flex items-center gap-1'">
            <UIcon name="heroicons-check-circle" class="w-4 h-4 shrink-0 text-green-600" />
            <!-- Per-user catalogs (OneDrive, personal Drive, mail) have nothing to
                 index admin-side — explain that instead of "Discovered 0 tables". -->
            <span v-if="indexing?.stats?.per_user_catalog">
                {{ $t('data.perUserIndexedHint', { items: itemNounPlural }) }}
            </span>
            <!-- Per-user OAuth connectors hold an OAuth client, not a token: there
                 is no connection-level identity to index with, so say so rather
                 than reporting a 401 nobody can act on. -->
            <span v-else-if="indexing?.stats?.awaiting_user_sign_in">
                {{ $t('data.perUserDiscoveredHint', { items: itemNounPlural.charAt(0).toUpperCase() + itemNounPlural.slice(1) }) }}
            </span>
            <span v-else>
                {{ $t('data.discoveredCount', { n: itemCount, noun: itemCount === 1 ? itemNoun : itemNounPlural }) }}
                <span v-if="indexing?.stats?.elapsed_s != null"> · {{ formatDuration(indexing.stats.elapsed_s) }}</span>
                <span v-if="indexing?.stats?.source_bytes" class="text-green-600/70"> · {{ formatBytes(indexing.stats.source_bytes) }}</span>
            </span>
        </div>

        <!-- Cancelled -->
        <div v-else-if="indexing?.status === 'cancelled'" class="text-xs text-gray-500 dark:text-gray-400 flex items-center gap-1">
            <UIcon name="heroicons-stop-circle" class="w-4 h-4" />
            <span>{{ $t('data.indexingStopped') }}</span>
        </div>

        <!-- Failed -->
        <div v-else-if="indexing?.status === 'failed'" :class="compact ? 'status-body text-red-700' : 'text-xs text-red-700'">
            <div class="flex items-center gap-1">
                <UIcon name="heroicons-exclamation-triangle" class="w-4 h-4" />
                <span class="font-medium">{{ $t(compact ? 'data.schemaRefreshFailed' : 'data.indexingFailed') }}</span>
            </div>
            <div v-if="indexing?.error && !compact" class="mt-1 text-red-600 break-words">
                {{ indexing.error }}
            </div>
        </div>

        <p v-if="elapsed !== null && isActive" class="text-xs text-gray-400">{{ $t('data.setupElapsed', { time: formatDuration(elapsed) }) }}</p>
        <div v-if="previousPhases.length && isActive" class="text-xs text-gray-400 space-y-1">
            <div>{{ $t('data.setupPreviousStages') }}</div>
            <div v-for="phase in previousPhases" :key="phase!" class="flex items-center gap-1.5"><UIcon name="i-heroicons-chevron-right" class="w-3 h-3" />{{ phaseLabel(phase) }}</div>
        </div>
        <div v-if="compact && visibleProblems.length" class="space-y-2" role="status">
            <p v-for="(event, i) in visibleProblems" :key="i" class="text-xs whitespace-pre-wrap break-words line-clamp-2" :class="event.level === 'error' ? 'text-red-600' : 'text-amber-700 dark:text-amber-400'">
                <UIcon name="heroicons-exclamation-triangle" class="w-4 h-4 inline-block align-text-bottom me-1" />{{ event.message }}
            </p>
        </div>
        <!-- Logs toggle -->
        <div v-if="showLogs && diagnosticEvents.length > 0" class="pt-1">
            <button
                type="button"
                class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 inline-flex items-center gap-1"
                @click="logsOpen = !logsOpen"
            >
                <UIcon :name="logsOpen ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3" />
                {{ logsOpen ? $t('data.hideLogs', { n: diagnosticEvents.length }) : $t(compact ? 'data.detailsAndLogs' : 'data.showLogs', { n: diagnosticEvents.length }) }}
            </button>
            <div v-if="logsOpen" class="mt-2 max-h-48 overflow-y-auto rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-2 text-[11px] font-mono text-gray-700 dark:text-gray-300 space-y-0.5">
                <div v-for="(ev, i) in diagnosticEvents" :key="i" class="flex gap-2">
                    <span class="text-gray-400 dark:text-gray-600 flex-none">{{ formatTs(ev.ts) }}</span>
                    <span class="min-w-0 break-words whitespace-pre-wrap" :class="levelClass(ev.level)">{{ ev.message }}</span>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { sameConnectionDiagnostic } from '~/utils/connectionDiagnostics'
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import {
    isIndexingActive,
    type ConnectionIndexing,
} from '~/composables/useConnectionStatus'

const diagnosticEvents = computed(() => {
    const events = [...(props.indexing?.events || [])]
    const error = props.indexing?.error
    if (!props.compact) return events
    const unique = events.filter((event, i) => !['warning', 'warn', 'error'].includes(event.level) || !events.slice(0, i).some(previous => previous.level === event.level && sameConnectionDiagnostic(previous.message, event.message)))
    if (error && !unique.some(event => sameConnectionDiagnostic(event.message, error))) unique.push({ ts: props.indexing?.finished_at || '', level: 'error', message: error })
    return unique
})
const visibleProblems = computed(() => props.indexing?.status === 'failed' ? [] : diagnosticEvents.value.filter(e => ['warning', 'warn', 'error'].includes(e.level || '')).slice(-3))

const props = withDefaults(defineProps<{
    indexing?: ConnectionIndexing | null
    compact?: boolean
    showLogs?: boolean
    allowCancel?: boolean
    cancelling?: boolean
}>(), {
    indexing: null,
    showLogs: true,
    allowCancel: false,
    cancelling: false,
})

defineEmits<{ (e: 'cancel'): void }>()

const { t, te } = useI18n()
const logsOpen = ref(false)
watch(() => props.indexing?.status, status => { if (status === 'failed' && !props.compact) logsOpen.value = true }, { immediate: true })
const now = ref(Date.now())
let elapsedTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => { elapsedTimer = setInterval(() => { now.value = Date.now() }, 1000) })
onBeforeUnmount(() => clearInterval(elapsedTimer))
const elapsed = computed(() => {
  const started = props.indexing?.started_at
  if (!started) return null
  const parsed = Date.parse(/(?:Z|[+-]\d{2}:\d{2})$/.test(started) ? started : started + 'Z')
  return Number.isFinite(parsed) ? Math.max(0, (now.value - parsed) / 1000) : null
})

function formatBytes(n?: number | null): string {
    if (!n || n <= 0) return ''
    const units = ['B', 'KB', 'MB', 'GB', 'TB']
    let size = n
    let i = 0
    while (size >= 1024 && i < units.length - 1) {
        size /= 1024
        i++
    }
    return `${i === 0 ? Math.round(size) : size.toFixed(1)} ${units[i]}`
}

function formatDuration(seconds?: number | null): string {
    if (seconds == null) return ''
    if (seconds < 60) return `${Math.round(seconds)}s`
    const m = Math.floor(seconds / 60)
    const s = Math.round(seconds % 60)
    if (m < 60) return s ? `${m}m ${s}s` : `${m}m`
    const h = Math.floor(m / 60)
    return `${h}h ${m % 60}m`
}

// Count + noun for the completed line. Newer runs carry the shape-aware noun
// in stats (item_noun / item_noun_plural — "files", "model tables", "tools");
// older runs only have the tool_count/table_count binary, so fall back to it.
const itemCount = computed(() => {
    const s = props.indexing?.stats
    if (s?.tool_count != null) return s.tool_count
    return s?.table_count ?? 0
})
const itemNoun = computed(() => {
    const s = props.indexing?.stats
    if (s?.item_noun) return s.item_noun
    return s?.tool_count != null ? t('data.nounTool') : t('data.nounTable')
})
const itemNounPlural = computed(() => {
    const s = props.indexing?.stats
    if (s?.item_noun_plural) return s.item_noun_plural
    if (s?.item_noun) return `${s.item_noun}s`
    return s?.tool_count != null ? t('data.nounTools') : t('data.nounTables')
})

const isActive = computed(() => isIndexingActive(props.indexing))
const hasTotal = computed(() => (props.indexing?.progress_total || 0) > 0)
const percent = computed(() => {
    const total = props.indexing?.progress_total || 0
    const done = props.indexing?.progress_done || 0
    if (total <= 0) return 0
    return Math.min(100, Math.floor((done / total) * 100))
})
function phaseLabel(phase?: string | null) {
  const key = `data.setupPhases.${phase || 'discovering'}`
  return te(key) ? t(key) : String(phase).replace(/_/g, ' ')
}
const previousPhases = computed(() => [...new Set((props.indexing?.events || []).map(ev => ev.phase).filter(Boolean))].filter(phase => phase !== props.indexing?.phase))
const summary = computed(() => {
  const idx = props.indexing
  const label = phaseLabel(idx?.phase)
  const count = idx?.progress_total ? ` (${idx.progress_done}/${idx.progress_total})` : ''
  return `${label}${idx?.current_item ? ` · ${idx.current_item}` : ''}${count}`
})

// Render the indexing log time in the org timezone (UTC-correct parse), keeping
// the HH:MM:SS precision the live log uses.
const { format: formatTime24 } = useFormatDate()
function formatTs(ts: string): string {
    if (!ts) return ''
    return formatTime24(ts, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}

function levelClass(level?: string): string {
    if (level === 'error') return 'text-red-600'
    if (level === 'warn') return 'text-amber-600'
    return 'text-gray-700'
}
</script>
