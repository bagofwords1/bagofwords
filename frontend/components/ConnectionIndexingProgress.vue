<template>
    <div class="space-y-2">
        <!-- Running / pending state -->
        <template v-if="isActive">
            <div class="flex items-center justify-between text-xs text-blue-700">
                <span class="font-medium">{{ summary }}</span>
                <span v-if="hasTotal">{{ percent }}%</span>
            </div>
            <div class="h-1.5 w-full bg-blue-100 rounded overflow-hidden">
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
                Discovered {{ indexing?.stats?.table_count ?? 0 }} table{{ (indexing?.stats?.table_count ?? 0) === 1 ? '' : 's' }}
                <span v-if="indexing?.stats?.elapsed_s != null"> in {{ indexing.stats.elapsed_s }}s</span>
            </span>
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
                class="text-[11px] text-gray-500 hover:text-gray-700 inline-flex items-center gap-1"
                @click="logsOpen = !logsOpen"
            >
                <UIcon :name="logsOpen ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3" />
                {{ logsOpen ? 'Hide' : 'Show' }} logs ({{ indexing.events.length }})
            </button>
            <div v-if="logsOpen" class="mt-2 max-h-48 overflow-y-auto rounded border border-gray-200 bg-gray-50 p-2 text-[11px] font-mono text-gray-700 space-y-0.5">
                <div v-for="(ev, i) in indexing.events" :key="i" class="flex gap-2">
                    <span class="text-gray-400 flex-none">{{ formatTs(ev.ts) }}</span>
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
}>(), {
    indexing: null,
    showLogs: true,
})

const logsOpen = ref(false)

const isActive = computed(() => isIndexingActive(props.indexing))
const hasTotal = computed(() => (props.indexing?.progress_total || 0) > 0)
const percent = computed(() => {
    const total = props.indexing?.progress_total || 0
    const done = props.indexing?.progress_done || 0
    if (total <= 0) return 0
    return Math.min(100, Math.floor((done / total) * 100))
})
const summary = computed(() => indexingSummary(props.indexing))

function formatTs(ts: string): string {
    if (!ts) return ''
    const d = new Date(ts)
    if (isNaN(d.getTime())) return ts
    const hh = String(d.getHours()).padStart(2, '0')
    const mm = String(d.getMinutes()).padStart(2, '0')
    const ss = String(d.getSeconds()).padStart(2, '0')
    return `${hh}:${mm}:${ss}`
}

function levelClass(level?: string): string {
    if (level === 'error') return 'text-red-600'
    if (level === 'warn') return 'text-amber-600'
    return 'text-gray-700'
}
</script>
