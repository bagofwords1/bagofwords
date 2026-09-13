<template>
    <div class="mt-4 bg-white dark:bg-gray-900 shadow-sm border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
        <div class="overflow-x-auto">
            <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                <thead class="bg-gray-50 dark:bg-gray-900">
                    <tr>
                        <th v-for="col in columns" :key="col.key" :class="['px-3 py-3 text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider whitespace-nowrap', col.align === 'right' ? 'text-end' : 'text-start']">
                            <button v-if="col.sort" type="button" class="inline-flex items-center gap-1 uppercase tracking-wider hover:text-gray-700 dark:hover:text-gray-200" @click="$emit('sort', col.sort)">
                                {{ col.label }}
                                <UIcon v-if="sort === col.sort" :name="dir === 'asc' ? 'i-heroicons-chevron-up' : 'i-heroicons-chevron-down'" class="w-3 h-3" />
                            </button>
                            <span v-else>{{ col.label }}</span>
                        </th>
                    </tr>
                </thead>
                <tbody class="divide-y divide-gray-200 dark:divide-gray-700 text-xs">
                    <template v-for="item in items" :key="item.id">
                        <tr class="hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer" data-testid="run-row" @click="$emit('open', item)">
                            <td class="px-3 py-3 whitespace-nowrap text-gray-500 dark:text-gray-400 align-top tabular-nums">{{ fmtWhen(item.created_at, locale) }}</td>
                            <td class="px-3 py-3 align-top">
                                <UTooltip v-if="item.status === 'stale'" :text="$t('monitoring.diagnosis.staleHelp')">
                                    <span class="inline-flex px-2 py-0.5 rounded-full font-medium" :class="statusClass(item.status)">{{ $t('monitoring.diagnosis.statusStale') }}</span>
                                </UTooltip>
                                <span v-else class="inline-flex px-2 py-0.5 rounded-full font-medium" :class="statusClass(item.status)">{{ item.status }}</span>
                            </td>
                            <!-- w-full + max-w-0 lets this column absorb the remaining width and
                                 truncate, so the fixed columns to its right always fit. -->
                            <td class="px-3 py-3 align-top w-full max-w-0">
                                <div class="flex items-start gap-2 min-w-0">
                                    <button type="button" class="mt-0.5 text-gray-400 hover:text-gray-700 flex-shrink-0" :aria-label="$t('monitoring.diagnosis.expandRow')" data-testid="expand-row" @click.stop="toggle(item)">
                                        <UIcon :name="expanded.has(item.id) ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3" />
                                    </button>
                                    <div class="min-w-0">
                                        <div class="flex items-center gap-1.5 text-gray-900 dark:text-white">
                                            <UTooltip v-if="platformOf(item)" :text="platformOf(item)!.label">
                                                <img v-if="platformOf(item)!.img" :src="platformOf(item)!.img!" class="h-3.5 w-3.5 inline flex-shrink-0" :alt="platformOf(item)!.label" />
                                                <UIcon v-else :name="platformOf(item)!.icon!" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500" />
                                            </UTooltip>
                                            <span class="truncate" :title="item.prompt">{{ item.prompt || '—' }}</span>
                                        </div>
                                        <div class="flex items-center gap-1.5 mt-0.5 text-[11px] text-gray-400 truncate">
                                            <button v-if="item.report.id" type="button" class="text-gray-500 hover:text-blue-600 hover:underline truncate" :title="$t('monitoring.diagnosis.pivotReport')" @click.stop="$emit('pivot', `report_id:${item.report.id}`)">{{ item.report.title || item.report.id }}</button>
                                            <template v-if="item.report.turn">
                                                <span>·</span>
                                                <span class="whitespace-nowrap">{{ $t('monitoring.diagnosis.turnOf', { n: item.report.turn, m: item.report.turns || item.report.turn }) }}</span>
                                            </template>
                                            <template v-if="item.error">
                                                <span>·</span>
                                                <span class="text-red-700 dark:text-red-400 truncate font-mono" :title="item.error">{{ item.error }}</span>
                                            </template>
                                        </div>
                                    </div>
                                </div>
                            </td>
                            <td class="px-3 py-3 whitespace-nowrap align-top text-gray-900 dark:text-white max-w-[120px] truncate">
                                <button type="button" class="hover:underline truncate max-w-full" :title="item.user.email || ''" @click.stop="$emit('pivot', item.user.name ? `user:${quote(item.user.name)}` : '')">{{ item.user.name || '—' }}</button>
                            </td>
                            <td class="px-3 py-3 whitespace-nowrap align-top text-gray-900 dark:text-white max-w-[160px]" :title="item.agents.map(a => a.name).join(', ')">
                                <div v-if="item.agents.length" class="flex items-center gap-1.5 min-w-0">
                                    <button
                                        v-for="a in item.agents"
                                        :key="a.id"
                                        type="button"
                                        class="inline-flex items-center gap-1 min-w-0 hover:underline"
                                        :class="item.agents.length > 1 ? 'max-w-[72px]' : 'max-w-full'"
                                        :title="a.name"
                                        data-testid="agent-chip"
                                        @click.stop="$emit('pivot', `agent:${quote(a.name)}`)"
                                    >
                                        <DataSourceIcon :type="a.type" :connector-key="a.connector_key" :icon-token="a.icon_token" :icon="a.icon" class="h-3.5 w-3.5 flex-shrink-0" />
                                        <span class="truncate">{{ a.name }}</span>
                                    </button>
                                </div>
                                <span v-else class="text-gray-400">—</span>
                            </td>
                            <td class="px-3 py-3 whitespace-nowrap align-top">
                                <UTooltip v-if="!item.indexed" :text="$t('monitoring.diagnosis.notIndexed')"><span class="text-gray-300 dark:text-gray-600">…</span></UTooltip>
                                <div v-else-if="item.tools.total" class="flex items-center gap-2">
                                    <div class="flex gap-0.5">
                                        <span v-for="i in Math.min(item.tools.total, 6)" :key="i" class="inline-block w-2 h-2 rounded-sm" :class="i <= item.tools.failed ? 'bg-red-500' : 'bg-green-500'"></span>
                                    </div>
                                    <span class="text-gray-500">{{ item.tools.total }}</span>
                                    <span v-if="item.tools.failed" class="text-red-700 dark:text-red-400">· {{ $t('monitoring.diagnosis.nFailed', { n: item.tools.failed }) }}</span>
                                </div>
                                <span v-else class="text-gray-400">—</span>
                            </td>
                            <td class="px-3 py-3 whitespace-nowrap align-top text-end tabular-nums text-gray-900 dark:text-white">{{ fmtDuration(item.duration_ms) }}</td>
                            <td class="px-3 py-3 whitespace-nowrap align-top text-end tabular-nums text-gray-900 dark:text-white" :title="item.model ? `${item.model} · ${fmtTokens(item.tokens)} tokens` : (item.indexed ? '' : $t('monitoring.diagnosis.notIndexed'))">
                                <span v-if="!item.indexed" class="text-gray-300 dark:text-gray-600">…</span>
                                <template v-else>{{ fmtCost(item.cost_usd, item.cost_is_partial) }}</template>
                            </td>
                            <td class="px-3 py-3 whitespace-nowrap align-top">
                                <UTooltip :text="judgeText(item)">
                                    <span v-if="item.feedback !== 'none'" class="inline-flex px-2 py-0.5 rounded-full font-medium" :class="item.feedback === 'positive' ? 'bg-green-100 dark:bg-green-900/50 text-green-800 dark:text-green-300' : 'bg-red-100 dark:bg-red-900/50 text-red-800 dark:text-red-300'">
                                        {{ item.feedback === 'positive' ? $t('monitoring.diagnosis.feedbackPositive') : $t('monitoring.diagnosis.feedbackNegative') }}
                                    </span>
                                    <span v-else class="text-gray-400">—</span>
                                </UTooltip>
                            </td>
                        </tr>
                        <!-- Child rows: the run's tool calls -->
                        <tr v-if="expanded.has(item.id)" :key="item.id + ':calls'" data-testid="tool-calls">
                            <td colspan="9" class="p-0">
                                <div v-if="!calls[item.id]" class="px-12 py-2 text-[11px] text-gray-400 bg-gray-50 dark:bg-gray-800/50">{{ $t('monitoring.diagnosis.loadingCalls') }}</div>
                                <div v-else-if="!calls[item.id].length" class="px-12 py-2 text-[11px] text-gray-400 bg-gray-50 dark:bg-gray-800/50">{{ $t('monitoring.diagnosis.noCalls') }}</div>
                                <div
                                    v-for="c in calls[item.id]"
                                    :key="c.id"
                                    class="flex items-center gap-4 h-9 ps-10 pe-4 border-s-2 ms-4 text-xs cursor-pointer group"
                                    :class="matched(item, c) ? 'bg-red-50/60 dark:bg-red-950/30 border-red-300 dark:border-red-800 hover:bg-red-50' : 'bg-gray-50 dark:bg-gray-800/50 border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800'"
                                    :title="$t('monitoring.diagnosis.openCall')"
                                    data-testid="tool-call-row"
                                    @click.stop="$emit('openCall', item, c)"
                                >
                                    <span class="w-[64px] flex-shrink-0 text-gray-500 tabular-nums whitespace-nowrap">{{ fmtTime(c.started_at, locale) }}</span>
                                    <span class="w-[70px] flex-shrink-0"><span class="inline-flex px-2 py-0.5 rounded-full font-medium" :class="statusClass(c.status)">{{ c.status === 'success' ? 'ok' : c.status }}</span></span>
                                    <span class="w-[200px] flex-shrink-0 font-mono text-gray-900 dark:text-white truncate">{{ c.tool }}<span v-if="c.action" class="text-gray-400"> · </span><span v-if="c.action" class="text-gray-500">{{ c.action }}</span></span>
                                    <span class="w-11 flex-shrink-0 text-end text-gray-500 tabular-nums">{{ c.attempt }}<span v-if="c.max_retries"> / {{ c.max_retries + 1 }}</span></span>
                                    <span class="w-14 flex-shrink-0 text-end tabular-nums text-gray-900 dark:text-white">{{ fmtDuration(c.duration_ms) }}</span>
                                    <!-- What the tool was asked, the tables it touched, and what it said -->
                                    <span class="flex items-center gap-2 min-w-0 flex-grow">
                                        <span v-if="c.args_preview" class="font-mono text-[11px] text-gray-600 dark:text-gray-300 truncate max-w-[45%]" :title="c.args_preview" data-testid="call-args">{{ c.args_preview }}</span>
                                        <button
                                            v-for="tbl in c.tables"
                                            :key="tbl"
                                            type="button"
                                            class="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300 text-[11px] font-mono whitespace-nowrap hover:bg-blue-100 flex-shrink-0"
                                            :title="$t('monitoring.diagnosis.tableTitle', { table: tbl })"
                                            data-testid="call-table"
                                            @click.stop="$emit('pivot', `table:${quote(tbl)}`)"
                                        ><UIcon name="i-heroicons-table-cells" class="w-3 h-3" />{{ tbl }}</button>
                                        <span v-if="c.error" class="font-mono text-[11px] text-red-700 dark:text-red-400 truncate" :title="c.error">{{ c.error }}</span>
                                        <span v-else-if="c.output_preview" class="text-[11px] text-gray-500 truncate" :title="c.result_summary || c.output_preview" data-testid="call-output">→ {{ c.output_preview }}</span>
                                    </span>
                                    <UIcon name="i-heroicons-arrow-top-right-on-square" class="w-3.5 h-3.5 text-gray-300 group-hover:text-gray-500 flex-shrink-0" />
                                </div>
                            </td>
                        </tr>
                    </template>
                </tbody>
            </table>
        </div>

        <div v-if="!items.length && !loading" class="text-center py-12">
            <UIcon name="i-heroicons-magnifying-glass" class="mx-auto h-10 w-10 text-gray-300 dark:text-gray-600" />
            <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">{{ $t('monitoring.diagnosis.emptyTitle') }}</h3>
            <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ $t('monitoring.diagnosis.emptySubtitle') }}</p>
            <div class="mt-3 flex items-center justify-center gap-2 text-xs">
                <button v-for="ex in examples" :key="ex" type="button" class="font-mono px-2 py-1 rounded bg-gray-100 dark:bg-gray-800 text-blue-700 dark:text-blue-300 hover:bg-gray-200" @click="$emit('example', ex)">{{ ex }}</button>
            </div>
        </div>

        <div v-if="nextCursor" class="border-t border-gray-200 dark:border-gray-700 px-4 py-2 flex items-center justify-between text-xs text-gray-500">
            <span>{{ $t('monitoring.diagnosis.showingOf', { n: items.length, total }) }}</span>
            <UButton size="xs" color="gray" variant="ghost" :loading="loadingMore" @click="$emit('more')">{{ $t('monitoring.diagnosis.loadMore') }}</UButton>
        </div>
    </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { fmtCost, fmtDuration, fmtTime, fmtTokens, fmtWhen, type RunItem, type ToolCall } from '~/composables/useDiagnosisQuery'

const props = defineProps<{
    items: RunItem[]
    calls: Record<string, ToolCall[]>
    loading: boolean
    loadingMore: boolean
    nextCursor: string | null
    total: number
    sort: string
    dir: 'asc' | 'desc'
}>()
const emit = defineEmits<{
    (e: 'open', item: RunItem): void
    (e: 'openCall', item: RunItem, call: ToolCall): void
    (e: 'expand', item: RunItem): void
    (e: 'pivot', terms: string): void
    (e: 'sort', key: string): void
    (e: 'more'): void
    (e: 'example', q: string): void
}>()
const { t, locale } = useI18n()

const columns = computed(() => [
    { key: 'time', label: t('monitoring.diagnosis.colTime'), sort: 'created' },
    { key: 'status', label: t('monitoring.diagnosis.colStatus') },
    { key: 'prompt', label: t('monitoring.diagnosis.colPrompt') },
    { key: 'user', label: t('monitoring.diagnosis.colUser') },
    { key: 'agent', label: t('monitoring.diagnosis.colAgent') },
    { key: 'tools', label: t('monitoring.diagnosis.colTools'), sort: 'tools.failed' },
    { key: 'duration', label: t('monitoring.diagnosis.colDuration'), sort: 'duration', align: 'right' },
    { key: 'cost', label: t('monitoring.diagnosis.colCost'), sort: 'cost', align: 'right' },
    { key: 'feedback', label: t('monitoring.diagnosis.colFeedback') },
])

const examples = ['status:error', 'tool:create_data tool.status:error', 'duration:>30s', 'feedback:negative']

const expanded = ref<Set<string>>(new Set())
// Matched rows (a tool.* query) open on load; reset when the page changes.
watch(() => props.items, (items) => {
    const next = new Set<string>()
    for (const it of items) if (it.matched_tool_call_ids.length && (expanded.value.has(it.id) || !props.calls[it.id])) next.add(it.id)
    for (const it of items) if (expanded.value.has(it.id)) next.add(it.id)
    expanded.value = next
    for (const it of items) if (next.has(it.id) && !props.calls[it.id]) emit('expand', it)
}, { immediate: true })

const toggle = (item: RunItem) => {
    const next = new Set(expanded.value)
    if (next.has(item.id)) next.delete(item.id)
    else {
        next.add(item.id)
        if (!props.calls[item.id]) emit('expand', item)
    }
    expanded.value = next
}

const matched = (item: RunItem, c: ToolCall) => item.matched_tool_call_ids.includes(c.id)

const quote = (v: string) => (/[\s()"]/.test(v) ? `"${v}"` : v)

const statusClass = (s: string) => {
    if (s === 'error' || s === 'sigkill') return 'bg-red-100 dark:bg-red-900/50 text-red-800 dark:text-red-300'
    if (s === 'success') return 'bg-green-100 dark:bg-green-900/50 text-green-800 dark:text-green-300'
    if (s === 'stale' || s === 'stopped') return 'bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-300'
    return 'bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300'
}

const judgeText = (item: RunItem) => {
    const j = item.judge
    const parts = [
        `${t('monitoring.diagnosis.judgeConfidence')} ${j.confidence ?? '—'}`,
        `${t('monitoring.diagnosis.judgeInstructions')} ${j.instructions ?? '—'}`,
        `${t('monitoring.diagnosis.judgeContext')} ${j.context ?? '—'}`,
    ]
    if (item.feedback_message) parts.push(`"${item.feedback_message}"`)
    return parts.join(' · ')
}

const PLATFORM_LABELS: Record<string, string> = { slack: 'Slack', teams: 'Microsoft Teams', email: 'Email', mcp: 'MCP', api: 'API' }
const PLATFORM_ICON: Record<string, string> = { email: 'i-heroicons-envelope', mcp: 'i-heroicons-cpu-chip', api: 'i-heroicons-code-bracket' }
const platformOf = (item: RunItem) => {
    const p = (item.platform || 'web').toLowerCase()
    if (p === 'web' || !(p in PLATFORM_LABELS)) return null
    return { label: PLATFORM_LABELS[p], img: p in PLATFORM_ICON ? null : `/icons/${p}.png`, icon: PLATFORM_ICON[p] || null }
}
</script>
