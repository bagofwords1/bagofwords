<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50 flex flex-col gap-2">
            <div class="flex items-center justify-between">
                <div>
                    <h3 class="text-lg font-semibold text-gray-900">LLM Usage Cost</h3>
                    <p class="text-sm text-gray-500 mt-1">Model usage by cost and tokens</p>
                </div>
                <div class="inline-flex rounded-full border border-gray-200 bg-gray-50 p-0.5 gap-1">
                    <button
                        v-for="option in metricOptions"
                        :key="option.value"
                        class="px-2.5 py-0.5 text-xs font-medium rounded-full transition"
                        :class="selectedMetric === option.value ? 'bg-white shadow text-gray-900' : 'text-gray-500'"
                        @click="selectedMetric = option.value"
                    >
                        {{ option.label }}
                    </button>
                </div>
            </div>
            <div v-if="llmUsageData" class="text-sm text-gray-500">
                Total calls: <span class="font-semibold text-gray-900">{{ llmUsageData.total_calls.toLocaleString() }}</span>
                · Total cost: <span class="font-semibold text-gray-900">${{ llmUsageData.total_cost_usd.toFixed(2) }}</span>
                · Tokens: <span class="font-semibold text-gray-900">{{ (llmUsageData.total_prompt_tokens + llmUsageData.total_completion_tokens).toLocaleString() }}</span>
            </div>
        </div>
        <div class="p-6">
            <div class="h-80">
                <div v-if="isLoading" class="flex items-center justify-center h-full">
                    <div class="flex items-center space-x-2">
                        <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                        <span class="text-gray-600">Loading LLM usage...</span>
                    </div>
                </div>
                <VChart
                    v-else-if="chartOptions"
                    class="chart"
                    :option="chartOptions"
                    autoresize
                />
                <div v-else class="flex items-center justify-center h-full text-gray-500">
                    No LLM usage data available
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart } from 'echarts/charts'
import {
    TooltipComponent,
    GridComponent,
    DatasetComponent,
    TransformComponent,
} from 'echarts/components'
import type { EChartsOption } from 'echarts'

use([
    CanvasRenderer,
    BarChart,
    TooltipComponent,
    GridComponent,
    DatasetComponent,
    TransformComponent,
])

interface LlmUsageItem {
    llm_model_id: string
    model_name: string
    model_id: string
    provider_type: string
    total_calls: number
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    input_cost_usd: number
    output_cost_usd: number
    total_cost_usd: number
}

interface DateRange { start: string; end: string }

interface LlmUsageMetrics {
    items: LlmUsageItem[]
    total_calls: number
    total_prompt_tokens: number
    total_completion_tokens: number
    total_cost_usd: number
    date_range: DateRange
}

interface Props {
    llmUsageData: LlmUsageMetrics | null
    isLoading: boolean
}

const props = defineProps<Props>()

const metricOptions = [
    { label: 'Total Cost', value: 'cost' as const },
    { label: 'Total Tokens', value: 'tokens' as const },
]

const selectedMetric = ref<typeof metricOptions[number]['value']>('cost')

const metricLabel = computed(() => selectedMetric.value === 'cost' ? 'total cost (USD)' : 'total tokens')

const chartOptions = computed((): EChartsOption | null => {
    if (!props.llmUsageData?.items?.length) return null

    const items = [...props.llmUsageData.items]
    const metricField = selectedMetric.value === 'cost' ? 'total_cost_usd' : 'total_tokens'

    const ranked = items
        .sort((a, b) => (b[metricField] || 0) - (a[metricField] || 0))
        .slice(0, 8) // display top 8 models

    const data = ranked.map(item => Number(item[metricField] || 0))
    const categories = ranked.map(item => item.model_name || item.model_id)

    const maxValue = Math.max(1, ...data)
    const stepSize = Math.ceil(maxValue / 5) || 1

    const formatter = selectedMetric.value === 'cost'
        ? (value: number) => `$${value.toFixed(2)}`
        : (value: number) => value.toLocaleString()

    return {
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            formatter: (params: any) => {
                if (!params?.length) return ''
                const point = params[0]
                const item = ranked[point.dataIndex]
                const tokens = item ? item.total_tokens.toLocaleString() : '0'
                const cost = item ? `$${item.total_cost_usd.toFixed(2)}` : '$0.00'
                return `
                    <div class="text-sm">
                        <div class="font-semibold text-gray-900">${item.model_name || item.model_id}</div>
                        <div class="text-gray-600">Provider: ${item.provider_type}</div>
                        <div class="text-gray-600">Calls: ${item.total_calls.toLocaleString()}</div>
                        <div class="text-gray-600">Tokens: ${tokens}</div>
                        <div class="text-gray-600">Cost: ${cost}</div>
                    </div>
                `
            }
        },
        grid: { left: '3%', right: '4%', bottom: '15%', top: '5%', containLabel: true },
        xAxis: {
            type: 'category',
            data: categories,
            axisTick: { show: false },
            axisLabel: { color: '#666', fontSize: 11, rotate: 15, interval: 0 }
        },
        yAxis: {
            type: 'value',
            min: 0,
            max: Math.ceil(maxValue / stepSize) * stepSize,
            interval: stepSize,
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: {
                color: '#666',
                fontSize: 12,
                formatter: (value: number) => formatter(value)
            }
        },
        series: [{
            type: 'bar',
            data,
            barWidth: '55%',
            itemStyle: {
                color: selectedMetric.value === 'cost'
                    ? '#6366f1'
                    : {
                        type: 'linear',
                        x: 0,
                        y: 0,
                        x2: 0,
                        y2: 1,
                        colorStops: [
                            { offset: 0, color: '#0ea5e9' },
                            { offset: 1, color: '#22d3ee' }
                        ]
                    },
                borderRadius: [6, 6, 0, 0]
            }
        }]
    }
})
</script>

<style scoped>
.chart {
    width: 100%;
    height: 100%;
}
</style>

