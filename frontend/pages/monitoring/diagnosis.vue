<template>
    <div class="mt-6">
        <!-- One query bar. The string in it (mirrored to the URL) is the whole page state. -->
        <div class="flex items-start gap-3">
            <DiagnosisQueryBar
                ref="bar"
                :model-value="dq.text.value"
                :tokens="dq.tokens.value"
                :error="dq.parseError.value"
                :loading="dq.loading.value"
                :dirty="dq.text.value !== dq.committed.value"
                :facets="facetsForBar"
                @update:model-value="dq.setText"
                @run="dq.run()"
            />
            <DiagnosisFilterBuilder :facets="facetsForBar" :query="dq.committed.value" @add="addTerms" />
            <DiagnosisTimeRange :range="dq.range.value" @change="dq.setRange" />
        </div>
        <DiagnosisQuickFilters :query="dq.text.value" :quick-filters="quickFilters" @toggle="toggleChip" />

        <div v-if="dq.serverError.value" class="mt-3 text-xs text-red-700 dark:text-red-400">{{ dq.serverError.value }}</div>

        <DiagnosisActivityChart
            :buckets="dq.buckets.value"
            :granularity="dq.granularity.value"
            :summary="dq.summary.value"
            :total-in-range="dq.totalInRange.value"
            :range-label="rangeLabel"
            :ms="dq.lastMs.value"
            @select="onBucket"
        />

        <DiagnosisToolsStrip :tools="dq.tools.value" :query="dq.text.value" @pick="tool => addTerms(`tool:${tool}`)" />

        <DiagnosisRunsTable
            :items="dq.items.value"
            :calls="calls"
            :loading="dq.loading.value"
            :loading-more="dq.loadingMore.value"
            :next-cursor="dq.nextCursor.value"
            :total="dq.total.value"
            :sort="dq.sort.value"
            :dir="dq.dir.value"
            @open="openTrace"
            @expand="loadCalls"
            @pivot="terms => terms && addTerms(terms)"
            @sort="onSort"
            @more="dq.loadMore()"
            @example="q => dq.commit(q)"
        />

        <TraceModal
            v-model="showTrace"
            :report-id="traceItem?.report.id || ''"
            :completion-id="traceItem?.completion_id || ''"
        />
    </div>
</template>

<script setup lang="ts">
import TraceModal from '~/components/console/TraceModal.vue'
import DiagnosisActivityChart from '~/components/console/DiagnosisActivityChart.vue'
import DiagnosisQueryBar from '~/components/diagnosis/QueryBar.vue'
import DiagnosisFilterBuilder from '~/components/diagnosis/FilterBuilder.vue'
import DiagnosisQuickFilters from '~/components/diagnosis/QuickFilters.vue'
import DiagnosisTimeRange from '~/components/diagnosis/TimeRange.vue'
import DiagnosisToolsStrip from '~/components/diagnosis/ToolsStrip.vue'
import DiagnosisRunsTable from '~/components/diagnosis/RunsTable.vue'
import { useDiagnosisQuery, type Bucket, type RunItem, type ToolCall } from '~/composables/useDiagnosisQuery'
import { addTerms as addTermsTo, removeTerms } from '~/utils/diagnosisQuery'
import type { Chip } from '~/components/diagnosis/QuickFilters.vue'

definePageMeta({
    auth: true,
    layout: 'monitoring',
    // Mirrors the /console/* gate: org admins see the org-wide console, agent
    // managers see it scoped to the agents they manage. Keep in step with
    // useCanAccessMonitoring().
    anyOf: ['manage_settings', 'manage_connections', { permission: 'manage', resourceType: 'data_source' }],
})

const { t } = useI18n()
const dq = useDiagnosisQuery()
const bar = ref<any>(null)

// Built-in quick filters come from the field registry endpoint so the chips
// can never drift from what the server understands.
const quickFilters = ref<{ id: string; q: string }[]>([])
onMounted(async () => {
    const meta = await dq.loadFields()
    quickFilters.value = meta?.quick_filters ?? []
    await dq.run()
})

const rangeLabel = computed(() => {
    const r = dq.range.value
    if (r.preset === '24h') return t('monitoring.diagnosis.range24h')
    if (r.preset === '7d') return t('monitoring.diagnosis.range7d')
    if (r.preset === '30d') return t('monitoring.diagnosis.range30d')
    if (r.preset === '90d') return t('monitoring.diagnosis.range90d')
    return `${r.start.toLocaleDateString()} – ${r.end.toLocaleDateString()}`
})

// Chips, the builder, the strip and row links all write terms into the query and run it.
const addTerms = (terms: string) => dq.commit(addTermsTo(dq.text.value, terms))
const toggleChip = (chip: Chip) => dq.commit(chip.active ? removeTerms(dq.text.value, chip.q) : addTermsTo(dq.text.value, chip.q))

const facetsForBar = (field: string, prefix: string, qWithoutTerm: string) => dq.facets(field, prefix, qWithoutTerm)

const onBucket = (b: Bucket, granularity: string) => {
    // A day bucket narrows to that day; an hour bucket to that hour; a week to its 7 days.
    if (granularity === 'day') return addTerms(`created:${b.bucket}`)
    if (granularity === 'week') {
        const end = new Date(b.bucket + 'T00:00:00')
        end.setDate(end.getDate() + 6)
        return addTerms(`created:${b.bucket}..${end.toISOString().slice(0, 10)}`)
    }
    return addTerms(`created:${b.bucket.slice(0, 10)}`)
}

const onSort = (key: string) => {
    const dir = dq.sort.value === key && dq.dir.value === 'desc' ? 'asc' : 'desc'
    dq.setSort(key, dir)
}

// Tool calls for expanded rows, fetched per page in one request.
const calls = reactive<Record<string, ToolCall[]>>({})
let pendingIds: string[] = []
let pendingTimer: ReturnType<typeof setTimeout> | null = null
const loadCalls = (item: RunItem) => {
    if (calls[item.id]) return
    pendingIds.push(item.id)
    if (pendingTimer) clearTimeout(pendingTimer)
    pendingTimer = setTimeout(async () => {
        const ids = Array.from(new Set(pendingIds))
        pendingIds = []
        const res = await dq.toolCalls(ids)
        for (const id of ids) calls[id] = res[id] ?? []
    }, 20)
}

const showTrace = ref(false)
const traceItem = ref<RunItem | null>(null)
const openTrace = (item: RunItem) => {
    traceItem.value = item
    showTrace.value = true
}
</script>
