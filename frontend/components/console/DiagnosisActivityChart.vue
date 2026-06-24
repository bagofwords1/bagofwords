<template>
    <div class="mb-6">
        <div class="flex items-center justify-between mb-2">
            <div class="flex items-baseline gap-2">
                <h3 class="text-sm font-semibold text-gray-900 dark:text-white">{{ $t('monitoring.diagnosis.activityChartTitle') }}</h3>
                <span class="text-xs text-gray-400 dark:text-gray-600">{{ $t('monitoring.diagnosis.activityChartSubtitle') }}</span>
            </div>
            <div v-if="totals" class="flex items-center gap-4 text-xs">
                <div class="flex items-center gap-1.5">
                    <span class="inline-block w-2.5 h-2.5 rounded-sm" style="background:#22c55e"></span>
                    <span class="text-gray-500 dark:text-gray-400">{{ $t('monitoring.diagnosis.activityChartSuccess') }}</span>
                    <span class="font-semibold text-gray-900 dark:text-white">{{ totals.success.toLocaleString() }}</span>
                </div>
                <div class="flex items-center gap-1.5">
                    <span class="inline-block w-2.5 h-2.5 rounded-sm" style="background:#ef4444"></span>
                    <span class="text-gray-500 dark:text-gray-400">{{ $t('monitoring.diagnosis.activityChartError') }}</span>
                    <span class="font-semibold text-gray-900 dark:text-white">{{ totals.error.toLocaleString() }}</span>
                </div>
            </div>
        </div>
        <div class="h-24">
            <div v-if="isLoading" class="flex items-center justify-center h-full">
                <div class="flex items-center space-x-2">
                    <div class="animate-spin rounded-full h-5 w-5 border-b-2 border-blue-600"></div>
                    <span class="text-gray-500 dark:text-gray-400 text-sm">{{ $t('monitoring.diagnosis.activityChartLoading') }}</span>
                </div>
            </div>
            <VChart
                v-else-if="chartOptions"
                :theme="colorMode.value === 'dark' ? 'dark' : undefined"
                class="chart"
                :option="chartOptions"
                autoresize
                @click="onBarClick"
            />
            <div v-else class="flex items-center justify-center h-full text-gray-400 dark:text-gray-600 text-sm">
                {{ $t('monitoring.diagnosis.activityChartEmpty') }}
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
const colorMode = useColorMode()
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart } from 'echarts/charts'
import {
    TooltipComponent,
    GridComponent,
    LegendComponent,
} from 'echarts/components'
import type { EChartsOption } from 'echarts'

use([
    CanvasRenderer,
    BarChart,
    TooltipComponent,
    GridComponent,
    LegendComponent,
])

interface DiagnosisStatusPoint {
    date: string
    success: number
    error: number
}

interface Props {
    points: DiagnosisStatusPoint[] | null
    isLoading: boolean
    selectedDate?: string | null
}

const props = defineProps<Props>()
const emit = defineEmits<{ (e: 'select-day', date: string): void }>()
const { t } = useI18n()
const _df = useFormatDate()

const onBarClick = (params: any) => {
    if (params?.dataIndex == null || !props.points?.length) return
    const point = props.points[params.dataIndex]
    if (point) emit('select-day', point.date)
}

const totals = computed(() => {
    if (!props.points?.length) return null
    return props.points.reduce(
        (acc, p) => ({ success: acc.success + (p.success || 0), error: acc.error + (p.error || 0) }),
        { success: 0, error: 0 }
    )
})

const chartOptions = computed((): EChartsOption | null => {
    if (!props.points?.length) return null
    // Nothing to show if every bucket is empty
    if (props.points.every(p => (p.success || 0) + (p.error || 0) === 0)) return null

    const points = props.points
    const dates = points.map(p => {
        const d = new Date(p.date)
        return `${d.getMonth() + 1}/${d.getDate()}`
    })
    const successData = points.map(p => p.success || 0)
    const errorData = points.map(p => p.error || 0)

    // When a day is selected, keep it vivid and fade the rest
    const selectedIdx = props.selectedDate ? points.findIndex(p => p.date === props.selectedDate) : -1
    const colorFor = (full: string, faded: string) =>
        selectedIdx < 0 ? full : (p: any) => (p.dataIndex === selectedIdx ? full : faded)

    // Thin out x-axis labels when there are many buckets
    const n = dates.length
    let interval = 0
    if (n > 60) interval = Math.floor(n / 12)
    else if (n > 30) interval = Math.floor(n / 10)
    else if (n > 14) interval = Math.floor(n / 8)

    const successLabel = t('monitoring.diagnosis.activityChartSuccess')
    const errorLabel = t('monitoring.diagnosis.activityChartError')
    const totalLabel = t('monitoring.diagnosis.activityChartTotal')

    return {
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            backgroundColor: 'rgba(30, 30, 30, 0.92)',
            borderColor: 'transparent',
            textStyle: { color: '#fff', fontSize: 12 },
            formatter: (params: any) => {
                if (!params?.length) return ''
                const idx = params[0].dataIndex
                const success = successData[idx] || 0
                const error = errorData[idx] || 0
                const total = success + error
                const fullDate = _df.formatDate(points[idx].date)
                return `
                    <div style="font-size:12px">
                        <div style="font-weight:600;margin-bottom:2px">${fullDate}</div>
                        <div><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:#22c55e;margin-right:6px"></span>${successLabel}: ${success.toLocaleString()}</div>
                        <div><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:#ef4444;margin-right:6px"></span>${errorLabel}: ${error.toLocaleString()}</div>
                        <div style="margin-top:2px;opacity:0.8">${totalLabel}: ${total.toLocaleString()}</div>
                    </div>
                `
            }
        },
        grid: { left: 8, right: 8, top: 8, bottom: 4, containLabel: true },
        xAxis: {
            type: 'category',
            data: dates,
            axisLine: { lineStyle: { color: '#e5e7eb' } },
            axisTick: { show: false },
            axisLabel: { interval, color: '#9ca3af', fontSize: 11 }
        },
        yAxis: {
            type: 'value',
            minInterval: 1,
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: { show: false },
            splitLine: { show: false }
        },
        series: [
            {
                name: successLabel,
                type: 'bar',
                stack: 'runs',
                data: successData,
                barWidth: '70%',
                barMaxWidth: 14,
                cursor: 'pointer',
                itemStyle: { color: colorFor('#22c55e', 'rgba(34,197,94,0.22)') }
            },
            {
                name: errorLabel,
                type: 'bar',
                stack: 'runs',
                data: errorData,
                barWidth: '70%',
                barMaxWidth: 14,
                cursor: 'pointer',
                itemStyle: { color: colorFor('#ef4444', 'rgba(239,68,68,0.22)'), borderRadius: [2, 2, 0, 0] }
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
