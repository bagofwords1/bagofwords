<template>
    <!-- Enterprise-gated: direct navigation without the license shows a notice
         instead of a broken page (the API returns 402). -->
    <div v-if="!usageLicensed" class="mt-6">
        <div class="bg-white border border-gray-200 rounded-xl p-12 text-center max-w-xl mx-auto">
            <UIcon name="i-heroicons-lock-closed" class="w-6 h-6 text-gray-400 mx-auto mb-3" />
            <h3 class="text-base font-semibold text-gray-900">{{ $t('monitoring.usage.lockedTitle') }}</h3>
            <p class="text-sm text-gray-500 mt-1.5">{{ $t('monitoring.usage.lockedBody') }}</p>
        </div>
    </div>

    <div v-else class="mt-6">
        <!-- Date range + agent (data source) filter -->
        <DateRangePicker
            :selected-period="selectedPeriod"
            :date-range="dateRange"
            @period-change="handlePeriodChange"
        >
            <AgentSelector :collapsed="false" :show-text="true" :show-label="false" />
        </DateRangePicker>

        <!-- KPI cards -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-6">
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">${{ formatNum(data?.total_cost_usd || 0, 2) }}</div>
                <div class="text-sm font-medium text-gray-600 mt-1">
                    {{ $t('monitoring.usage.kpiTotalCost') }}
                    <span v-if="hasEstimated" class="text-gray-400 font-normal">· {{ $t('monitoring.usage.estimated') }}</span>
                </div>
            </div>
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">{{ compactNum(data?.total_tokens || 0) }}</div>
                <div class="text-sm font-medium text-gray-600 mt-1">{{ $t('monitoring.usage.kpiTotalTokens') }}</div>
            </div>
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">{{ compactNum(data?.total_calls || 0) }}</div>
                <div class="text-sm font-medium text-gray-600 mt-1">{{ $t('monitoring.usage.kpiTotalCalls') }}</div>
            </div>
            <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
                <div class="text-2xl font-bold text-gray-900">${{ formatNum(avgCostPerCall, 4) }}</div>
                <div class="text-sm font-medium text-gray-600 mt-1">{{ $t('monitoring.usage.kpiAvgCostPerCall') }}</div>
            </div>
        </div>

        <!-- Trend chart -->
        <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden mb-6">
            <div class="p-6 border-b border-gray-50 flex items-center justify-between">
                <h3 class="text-lg font-semibold text-gray-900">{{ $t('monitoring.usage.trendTitle') }}</h3>
                <div class="inline-flex rounded-md border border-gray-200 overflow-hidden">
                    <button
                        v-for="opt in metricOptions"
                        :key="opt.value"
                        class="px-3 py-1 text-xs font-medium transition border-l first:border-l-0 border-gray-200"
                        :class="selectedMetric === opt.value ? 'bg-gray-100 text-gray-900' : 'bg-white text-gray-500 hover:text-gray-700'"
                        @click="selectedMetric = opt.value"
                    >
                        {{ opt.label }}
                    </button>
                </div>
            </div>
            <div class="p-6">
                <div class="h-72">
                    <div v-if="isLoading" class="flex items-center justify-center h-full">
                        <div class="flex items-center space-x-2">
                            <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                            <span class="text-gray-600">{{ $t('monitoring.usage.loading') }}</span>
                        </div>
                    </div>
                    <VChart v-else-if="trendOptions" class="chart" :option="trendOptions" autoresize />
                    <div v-else class="flex items-center justify-center h-full text-gray-500">
                        {{ $t('monitoring.usage.noData') }}
                    </div>
                </div>
            </div>
        </div>

        <!-- Breakdown -->
        <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
            <div class="p-6 border-b border-gray-50 flex items-center justify-between flex-wrap gap-3">
                <div>
                    <h3 class="text-lg font-semibold text-gray-900">{{ $t('monitoring.usage.breakdownTitle') }}</h3>
                </div>
                <div class="flex items-center gap-2">
                    <span class="text-sm font-medium text-gray-700">{{ $t('monitoring.usage.groupBy') }}:</span>
                    <USelectMenu
                        :model-value="selectedGroupBy"
                        :options="groupByOptions"
                        @update:model-value="onGroupByChange"
                        size="sm"
                        class="min-w-[180px]"
                    />
                    <UButton
                        icon="i-heroicons-arrow-down-tray"
                        color="gray"
                        variant="solid"
                        size="sm"
                        :disabled="!items.length"
                        @click="exportCsv"
                    >
                        {{ $t('monitoring.usage.exportCsv') }}
                    </UButton>
                </div>
            </div>

            <div class="overflow-x-auto">
                <table class="min-w-full">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.usage.colName') }}</th>
                            <th class="px-4 py-3 text-end text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.usage.colCalls') }}</th>
                            <th class="px-4 py-3 text-end text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.usage.colTokens') }}</th>
                            <th class="px-4 py-3 text-end text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.usage.colInputCost') }}</th>
                            <th class="px-4 py-3 text-end text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.usage.colOutputCost') }}</th>
                            <th class="px-4 py-3 text-end text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.usage.colTotalCost') }}</th>
                            <th class="px-6 py-3 text-end text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('monitoring.usage.colShare') }}</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200 text-sm">
                        <tr v-for="item in items" :key="item.key" class="hover:bg-gray-50">
                            <td class="px-6 py-4">
                                <div class="font-medium text-gray-900">{{ item.label }}</div>
                                <div v-if="item.sublabel" class="text-xs text-gray-500">{{ item.sublabel }}</div>
                            </td>
                            <td class="px-4 py-4 text-end text-gray-600 tabular-nums">{{ compactNum(item.total_calls) }}</td>
                            <td class="px-4 py-4 text-end text-gray-600 tabular-nums">{{ compactNum(item.total_tokens) }}</td>
                            <td class="px-4 py-4 text-end text-gray-600 tabular-nums">${{ formatNum(item.input_cost_usd, 4) }}</td>
                            <td class="px-4 py-4 text-end text-gray-600 tabular-nums">${{ formatNum(item.output_cost_usd, 4) }}</td>
                            <td class="px-4 py-4 text-end font-semibold text-gray-900 tabular-nums">${{ formatNum(item.total_cost_usd, 4) }}</td>
                            <td class="px-6 py-4 text-end text-gray-500 tabular-nums">{{ sharePct(item) }}%</td>
                        </tr>
                        <tr v-if="!isLoading && items.length === 0">
                            <td colspan="7" class="px-6 py-12 text-center text-gray-500">{{ $t('monitoring.usage.noData') }}</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <div v-if="selectedGroupBy.value === 'data_source' || selectedGroupBy.value === 'group'"
                 class="px-6 py-3 text-xs text-gray-400 border-t border-gray-50">
                {{ $t('monitoring.usage.fanoutNote') }}
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { TooltipComponent, GridComponent } from 'echarts/components'
import type { EChartsOption } from 'echarts'
import DateRangePicker from '~/components/console/DateRangePicker.vue'
import AgentSelector from '~/components/AgentSelector.vue'

use([CanvasRenderer, LineChart, TooltipComponent, GridComponent])

const { t } = useI18n()
const { selectedAgents } = useAgent()
const { hasFeature } = useEnterprise()

// Usage is an enterprise feature; gate matches the backend `usage_dashboard` check.
const usageLicensed = computed(() => hasFeature('usage_dashboard'))

definePageMeta({
    auth: true,
    layout: 'monitoring',
    // Admin-only: matches the `manage_settings` gate on the /console/* endpoints.
    permissions: ['manage_settings']
})

interface CostBreakdownItem {
    key: string
    label: string
    sublabel?: string
    provider_type?: string
    total_calls: number
    prompt_tokens: number
    completion_tokens: number
    cache_read_tokens: number
    cache_creation_tokens: number
    total_tokens: number
    input_cost_usd: number
    output_cost_usd: number
    total_cost_usd: number
}
interface CostTimeSeriesPoint { date: string; cost_usd: number; tokens: number }
interface CostMetrics {
    group_by: string
    items: CostBreakdownItem[]
    timeseries: CostTimeSeriesPoint[]
    total_calls: number
    total_prompt_tokens: number
    total_completion_tokens: number
    total_tokens: number
    total_cost_usd: number
    has_estimated_provider: boolean
    date_range: { start: string; end: string }
}
interface DateRange { start: string; end: string }

const isLoading = ref(false)
const data = ref<CostMetrics | null>(null)

const selectedPeriod = ref({ label: t('monitoring.overview.last30d'), value: '30_days' })
const dateRange = ref<DateRange>({ start: '', end: '' })

const groupByOptions = computed(() => [
    { label: t('monitoring.usage.groupByModel'), value: 'model' },
    { label: t('monitoring.usage.groupByProvider'), value: 'provider' },
    { label: t('monitoring.usage.groupByUser'), value: 'user' },
    { label: t('monitoring.usage.groupByDataSource'), value: 'data_source' },
    { label: t('monitoring.usage.groupByGroup'), value: 'group' },
    { label: t('monitoring.usage.groupByScope'), value: 'scope' },
])
const selectedGroupBy = ref(groupByOptions.value[0])

const metricOptions = computed(() => [
    { label: t('monitoring.usage.metricCost'), value: 'cost' as const },
    { label: t('monitoring.usage.metricTokens'), value: 'tokens' as const },
])
const selectedMetric = ref<'cost' | 'tokens'>('cost')

const items = computed(() => data.value?.items || [])
const hasEstimated = computed(() => data.value?.has_estimated_provider ?? false)
const avgCostPerCall = computed(() => {
    const calls = data.value?.total_calls || 0
    return calls > 0 ? (data.value!.total_cost_usd / calls) : 0
})

const formatNum = (n: number, digits = 2) => Number(n || 0).toFixed(digits)
const compactNum = (n: number) => Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n || 0)
const sharePct = (item: CostBreakdownItem) => {
    const total = data.value?.total_cost_usd || 0
    if (total <= 0) return 0
    return Math.round((item.total_cost_usd / total) * 1000) / 10
}

const csvCell = (v: any) => {
    const s = String(v ?? '')
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

const exportCsv = () => {
    if (!data.value || !items.value.length) return
    const dim = selectedGroupBy.value.label
    const header = [dim, 'Detail', 'Provider', 'Calls', 'Prompt tokens', 'Completion tokens',
        'Cache read tokens', 'Cache creation tokens', 'Total tokens',
        'Input cost (USD)', 'Output cost (USD)', 'Total cost (USD)', 'Share %']
    const rows = items.value.map(i => [
        i.label, i.sublabel || '', i.provider_type || '', i.total_calls,
        i.prompt_tokens, i.completion_tokens, i.cache_read_tokens, i.cache_creation_tokens,
        i.total_tokens, i.input_cost_usd, i.output_cost_usd, i.total_cost_usd, sharePct(i),
    ])
    // Trailing total row so the export is self-checking.
    rows.push(['TOTAL', '', '', data.value.total_calls, data.value.total_prompt_tokens,
        data.value.total_completion_tokens, '', '', data.value.total_tokens,
        '', '', data.value.total_cost_usd, 100])
    const csv = [header, ...rows].map(r => r.map(csvCell).join(',')).join('\n')

    const range = data.value.date_range
    const fname = `usage_${selectedGroupBy.value.value}_${(range.start || '').slice(0, 10)}_${(range.end || '').slice(0, 10)}.csv`
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fname
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
}

const trendOptions = computed((): EChartsOption | null => {
    const series = data.value?.timeseries || []
    if (!series.length) return null
    const isCost = selectedMetric.value === 'cost'
    const dates = series.map(p => p.date)
    const values = series.map(p => isCost ? Number(p.cost_usd || 0) : Number(p.tokens || 0))
    const fmt = isCost ? (v: number) => `$${v.toFixed(2)}` : (v: number) => compactNum(v)
    return {
        tooltip: {
            trigger: 'axis',
            formatter: (params: any) => {
                const p = params?.[0]
                if (!p) return ''
                return `<div class="text-sm"><div class="font-semibold">${p.axisValue}</div><div>${isCost ? 'Cost' : 'Tokens'}: ${fmt(p.value)}</div></div>`
            }
        },
        grid: { left: '3%', right: '4%', bottom: '3%', top: '8%', containLabel: true },
        xAxis: {
            type: 'category', boundaryGap: false, data: dates,
            axisLabel: { color: '#666', fontSize: 11 }, axisTick: { show: false }
        },
        yAxis: {
            type: 'value', min: 0,
            axisLine: { show: false }, axisTick: { show: false },
            splitLine: { lineStyle: { color: '#f3f4f6' } },
            axisLabel: { color: '#666', fontSize: 11, formatter: (v: number) => fmt(v) }
        },
        series: [{
            name: isCost ? 'Cost' : 'Tokens',
            type: 'line', smooth: true, showSymbol: false, data: values,
            lineStyle: { color: '#3b82f6', width: 2 },
            areaStyle: { color: 'rgba(59,130,246,0.06)' },
            itemStyle: { color: '#3b82f6' }
        }]
    }
})

const appendDateParams = (params: URLSearchParams) => {
    if (dateRange.value.start) params.append('start_date', new Date(dateRange.value.start).toISOString())
    if (dateRange.value.end) params.append('end_date', new Date(dateRange.value.end).toISOString())
}

const fetchCost = async () => {
    if (!usageLicensed.value) return  // enterprise-gated; nothing to fetch
    isLoading.value = true
    try {
        const params = new URLSearchParams()
        params.append('group_by', selectedGroupBy.value.value)
        appendDateParams(params)
        if (selectedAgents.value.length > 0) {
            params.append('data_source_ids', selectedAgents.value.join(','))
        }
        const res = await useMyFetch<CostMetrics>(`/api/console/metrics/usage?${params}`)
        data.value = res.data.value || null
    } catch (e) {
        console.error('Failed to fetch cost metrics:', e)
        data.value = null
    } finally {
        isLoading.value = false
    }
}

const onGroupByChange = (val: { label: string; value: string }) => {
    selectedGroupBy.value = val
    fetchCost()
}

const handlePeriodChange = (period: { label: string; value: string }) => {
    selectedPeriod.value = period
    const end = new Date()
    let start: Date | null = null
    switch (period.value) {
        case '30_days': start = new Date(); start.setDate(start.getDate() - 30); break
        case '90_days': start = new Date(); start.setDate(start.getDate() - 90); break
        case 'all_time': default: start = null; break
    }
    dateRange.value = {
        start: start ? start.toISOString().split('T')[0] : '',
        end: end.toISOString().split('T')[0]
    }
    fetchCost()
}

watch(selectedAgents, () => fetchCost(), { deep: true })

onMounted(() => {
    handlePeriodChange(selectedPeriod.value)
})
</script>

<style scoped>
.chart { width: 100%; height: 100%; }
</style>
