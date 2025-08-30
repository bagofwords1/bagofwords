<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50">
            <h3 class="text-lg font-semibold text-gray-900">Tool Usage</h3>
            <p class="text-sm text-gray-500 mt-1">Key tools triggered by AI in this period</p>
        </div>
        <div class="p-6">
            <div class="h-80">
                <div v-if="isLoading" class="flex items-center justify-center h-full">
                    <div class="flex items-center space-x-2">
                        <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                        <span class="text-gray-600">Loading chart...</span>
                    </div>
                </div>
                <VChart
                    v-else-if="chartOptions"
                    class="chart"
                    :option="chartOptions"
                    autoresize
                />
                <div v-else class="flex items-center justify-center h-full text-gray-500">
                    No tool usage data available
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
    TitleComponent,
    TooltipComponent,
    GridComponent,
    LegendComponent,
} from 'echarts/components'
import type { EChartsOption } from 'echarts'

use([
    CanvasRenderer,
    BarChart,
    TitleComponent,
    TooltipComponent,
    GridComponent,
    LegendComponent,
])

interface ToolUsageItem {
    tool_name: string
    label: string
    count: number
}

interface DateRange { start: string; end: string }

interface ToolUsageMetrics {
    items: ToolUsageItem[]
    date_range: DateRange
}

interface Props {
    toolUsageData: ToolUsageMetrics | null
    isLoading: boolean
}

const props = defineProps<Props>()

const chartOptions = computed((): EChartsOption | null => {
    if (!props.toolUsageData?.items) return null

    const items = props.toolUsageData.items
    const data = items.map(i => i.count)
    const categories = items.map(i => i.label)
    const maxValue = Math.max(1, ...data)
    const stepSize = Math.ceil(maxValue / 5)

    return {
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: '3%', right: '4%', bottom: '15%', top: '5%', containLabel: true },
        xAxis: {
            type: 'category',
            data: categories,
            axisTick: { show: false },
            axisLabel: { color: '#666', fontSize: 11, rotate: 20, interval: 0 }
        },
        yAxis: {
            type: 'value',
            min: 0,
            max: Math.ceil(maxValue / stepSize) * stepSize,
            interval: stepSize,
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: { color: '#666', fontSize: 12 }
        },
        series: [{
            type: 'bar',
            data,
            barWidth: '50%',
            itemStyle: {
                color: {
                    type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                    colorStops: [
                        { offset: 0, color: '#34d399' },
                        { offset: 1, color: '#10b981' }
                    ]
                },
                borderRadius: [4, 4, 0, 0]
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


