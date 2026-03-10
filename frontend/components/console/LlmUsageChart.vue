<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50 flex flex-col gap-2">
            <div class="flex items-center justify-between">
                <div>
                    <h3 class="text-lg font-semibold text-gray-900">LLM Usage {{ hasEstimatedProvider ? 'Estimated Cost' : 'Cost' }}</h3>
                    <p class="text-sm text-gray-500 mt-1">Model usage by {{ hasEstimatedProvider ? 'estimated cost' : 'cost' }} and tokens</p>
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
                · {{ hasEstimatedProvider ? 'Est. cost' : 'Total cost' }}: <span class="font-semibold text-gray-900">${{ llmUsageData.total_cost_usd.toFixed(2) }}</span>
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
    LegendComponent,
} from 'echarts/components'
import type { EChartsOption } from 'echarts'

use([
    CanvasRenderer,
    BarChart,
    TooltipComponent,
    GridComponent,
    DatasetComponent,
    TransformComponent,
    LegendComponent,
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

const metricOptions = computed(() => [
    { label: hasEstimatedProvider.value ? 'Est. Cost' : 'Total Cost', value: 'cost' as const },
    { label: 'Total Tokens', value: 'tokens' as const },
])

const isEstimatedProvider = (providerType: string | undefined) => {
    const pt = (providerType || '').toLowerCase()
    return pt === 'custom' || pt === 'azure' || pt.startsWith('bedrock')
}

const hasEstimatedProvider = computed(() =>
    props.llmUsageData?.items?.some(item => isEstimatedProvider(item.provider_type)) ?? false
)

const defaultMetric = computed(() => {
    if (!props.llmUsageData?.items?.length) return 'cost'
    return hasEstimatedProvider.value ? 'tokens' : 'cost'
})

const selectedMetric = ref<'cost' | 'tokens'>(defaultMetric.value)

watch(defaultMetric, (val) => {
    selectedMetric.value = val
})

const chartOptions = computed((): EChartsOption | null => {
    if (!props.llmUsageData?.items?.length) return null

    const items = [...props.llmUsageData.items]
    const metricField = selectedMetric.value === 'cost' ? 'total_cost_usd' : 'total_tokens'
    const isCost = selectedMetric.value === 'cost'

    const ranked = items
        .sort((a, b) => (b[metricField] || 0) - (a[metricField] || 0))
        .slice(0, 8) // display top 8 models

    const categories = ranked.map(item => item.model_name || item.model_id)
    const inputData = ranked.map(item => Number(isCost ? item.input_cost_usd : item.prompt_tokens) || 0)
    const outputData = ranked.map(item => Number(isCost ? item.output_cost_usd : item.completion_tokens) || 0)

    const maxValue = Math.max(1, ...inputData.map((v, i) => v + outputData[i]))
    const stepSize = Math.ceil(maxValue / 5) || 1

    const formatter = isCost
        ? (value: number) => `$${value.toFixed(2)}`
        : (value: number) => value.toLocaleString()

    const inputLabel = isCost ? 'Input Cost' : 'Input Tokens'
    const outputLabel = isCost ? 'Output Cost' : 'Output Tokens'

    return {
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            formatter: (params: any) => {
                if (!params?.length) return ''
                const item = ranked[params[0].dataIndex]
                return `
                    <div class="text-sm">
                        <div class="font-semibold text-gray-900">${item.model_name || item.model_id}</div>
                        <div class="text-gray-600">Provider: ${item.provider_type}</div>
                        <div class="text-gray-600">Calls: ${item.total_calls.toLocaleString()}</div>
                        <div class="text-gray-600">Input tokens: ${item.prompt_tokens.toLocaleString()}</div>
                        <div class="text-gray-600">Output tokens: ${item.completion_tokens.toLocaleString()}</div>
                        <div class="text-gray-600">Input cost: $${item.input_cost_usd.toFixed(4)}</div>
                        <div class="text-gray-600">Output cost: $${item.output_cost_usd.toFixed(4)}</div>
                        <div class="font-semibold text-gray-900 mt-1">Total: ${formatter(isCost ? item.total_cost_usd : item.total_tokens)}</div>
                    </div>
                `
            }
        },
        legend: {
            data: [inputLabel, outputLabel],
            bottom: 0,
            textStyle: { color: '#666', fontSize: 12 }
        },
        grid: { left: '3%', right: '4%', bottom: '12%', top: '5%', containLabel: true },
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
        series: [
            {
                name: inputLabel,
                type: 'bar',
                stack: 'total',
                data: inputData,
                barWidth: '55%',
                itemStyle: { color: isCost ? '#818cf8' : '#22d3ee', borderRadius: [0, 0, 0, 0] }
            },
            {
                name: outputLabel,
                type: 'bar',
                stack: 'total',
                data: outputData,
                barWidth: '55%',
                itemStyle: { color: isCost ? '#4f46e5' : '#0284c7', borderRadius: [6, 6, 0, 0] }
            }
        ]
    }
})
</script>

<style scoped>
.chart {
    width: 100%;
    height: 100%;
}
</style>

