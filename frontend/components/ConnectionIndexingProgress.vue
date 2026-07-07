<template>
    <div class="space-y-2">
        <!-- Running / pending state -->
        <template v-if="isActive">
            <div class="flex items-center justify-between text-xs text-blue-700">
                <span class="font-medium truncate">{{ summary }}</span>
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
                        {{ cancelling ? 'Stopping…' : 'Stop' }}
                    </button>
                </div>
            </div>
            <div class="h-1.5 w-full bg-blue-100 dark:bg-blue-900/50 rounded overflow-hidden">
                <div
                    class="h-full bg-blue-500 transition-all duration-300"
                    :class="{ 'animate-pulse w-1/3': !hasTotal }"
                    :style="hasTotal ? { width: percent + '%' } : {}"
                ></div>
            </div>
        </template>

        <!-- Completed -->
        <div v-else-if="indexing?.status === 'completed'" class="text-xs text-green-700 flex items-center gap-1">
            <UIcon name="heroicons-check-circle" class="w-4 h-4" />
            <span>
                <template v-if="indexing?.stats?.tool_count != null">
                    Discovered {{ indexing.stats.tool_count }} tool{{ indexing.stats.tool_count === 1 ? '' : 's' }}
                </template>
                <template v-else>
                    Discovered {{ indexing?.stats?.table_count ?? 0 }} table{{ (indexing?.stats?.table_count ?? 0) === 1 ? '' : 's' }}
                </template>
                <span v-if="indexing?.stats?.elapsed_s != null"> in {{ formatDuration(indexing.stats.elapsed_s) }}</span>
                <span v-if="indexing?.stats?.source_bytes" class="text-green-600/70"> · {{ formatBytes(indexing.stats.source_bytes) }}</span>
            </span>
        </div>

        <!-- Cancelled -->
        <div v-else-if="indexing?.status === 'cancelled'" class="text-xs text-gray-500 dark:text-gray-400 flex items-center gap-1">
            <UIcon name="heroicons-stop-circle" class="w-4 h-4" />
            <span>Indexing stopped</span>
        </div>

        <!-- Failed -->
        <div v-else-if="indexing?.status === 'failed'" class="text-xs text-red-700">
            <div class="flex items-center gap-1">
                <UIcon name="heroicons-exclamation-triangle" class="w-4 h-4" />
                <span class="font-medium">Indexing failed</span>
            </div>
            <div v-if="indexing?.error" class="mt-1 text-red-600 break-words">
                {{ indexing.error }}
            </div>
        </div>

        <!-- Logs toggle -->
        <div v-if="showLogs && (indexing?.events?.length ?? 0) > 0" class="pt-1">
            <button
                type="button"
                class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 inline-flex items-center gap-1"
                @click="logsOpen = !logsOpen"
            >
                <UIcon :name="logsOpen ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3" />
                {{ logsOpen ? 'Hide' : 'Show' }} logs ({{ indexing.events.length }})
            </button>
            <div v-if="logsOpen" class="mt-2 max-h-48 overflow-y-auto rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-2 text-[11px] font-mono text-gray-700 dark:text-gray-300 space-y-0.5">
                <div v-for="(ev, i) in indexing.events" :key="i" class="flex gap-2">
                    <span class="text-gray-400 dark:text-gray-600 flex-none">{{ formatTs(ev.ts) }}</span>
                    <span :class="levelClass(ev.level)">{{ ev.message }}</span>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import {
    isIndexingActive,
    indexingSummary,
    type ConnectionIndexing,
} from '~/composables/useConnectionStatus'

const props = withDefaults(defineProps<{
    indexing?: ConnectionIndexing | null
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

const logsOpen = ref(false)

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

const isActive = computed(() => isIndexingActive(props.indexing))
const hasTotal = computed(() => (props.indexing?.progress_total || 0) > 0)
const percent = computed(() => {
    const total = props.indexing?.progress_total || 0
    const done = props.indexing?.progress_done || 0
    if (total <= 0) return 0
    return Math.min(100, Math.floor((done / total) * 100))
})
const summary = computed(() => indexingSummary(props.indexing))

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
