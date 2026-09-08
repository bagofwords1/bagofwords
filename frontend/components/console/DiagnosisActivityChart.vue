<template>
    <div class="mt-5">
        <div class="flex items-baseline justify-between">
            <div class="flex items-baseline gap-2.5 min-w-0">
                <span class="text-sm font-semibold text-gray-900 dark:text-white whitespace-nowrap">{{ $t('monitoring.diagnosis.matchLine', { n: summary?.matched ?? 0 }) }}</span>
                <span v-if="summary" class="text-xs text-gray-400 truncate">
                    {{ $t('monitoring.diagnosis.summaryLine', { total: totalInRange.toLocaleString(), range: rangeLabel, errors: summary.errors.toLocaleString(), p50: fmtDuration(summary.p50_ms), cost: fmtCost(summary.cost_usd), users: summary.users }) }}
                    <span v-if="ms != null" class="text-gray-300"> · {{ ms }}ms</span>
                </span>
                <UTooltip v-if="summary && summary.unindexed > 0" :text="$t('monitoring.diagnosis.unindexedHelp')">
                    <span class="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded bg-amber-50 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300 whitespace-nowrap" data-testid="unindexed-note">
                        <span class="animate-spin rounded-full h-2.5 w-2.5 border-b-2 border-amber-500"></span>
                        {{ $t('monitoring.diagnosis.unindexedNote', { n: summary.unindexed.toLocaleString() }) }}
                    </span>
                </UTooltip>
            </div>
            <div class="flex items-center gap-4 text-xs whitespace-nowrap">
                <div class="flex items-center gap-1.5"><span class="inline-block w-2.5 h-2.5 rounded-sm" style="background:#22c55e"></span><span class="text-gray-500">{{ $t('monitoring.diagnosis.legendMatched') }}</span></div>
                <div class="flex items-center gap-1.5"><span class="inline-block w-2.5 h-2.5 rounded-sm" style="background:#ef4444"></span><span class="text-gray-500">{{ $t('monitoring.diagnosis.legendMatchedErrors') }}</span></div>
                <div class="flex items-center gap-1.5"><span class="inline-block w-2.5 h-2.5 rounded-sm bg-gray-200 dark:bg-gray-700"></span><span class="text-gray-500">{{ $t('monitoring.diagnosis.legendOther') }}</span></div>
            </div>
        </div>
        <div class="h-24 mt-1">
            <VChart v-if="chartOptions" :theme="colorMode.value === 'dark' ? 'dark' : undefined" class="chart" :option="chartOptions" autoresize @click="onBarClick" />
            <div v-else class="flex items-center justify-center h-full text-gray-400 dark:text-gray-600 text-sm">{{ $t('monitoring.diagnosis.activityChartEmpty') }}</div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart } from 'echarts/charts'
import { TooltipComponent, GridComponent } from 'echarts/components'
import type { EChartsOption } from 'echarts'
import { fmtCost, fmtDuration, type Bucket, type Summary } from '~/composables/useDiagnosisQuery'

use([CanvasRenderer, BarChart, TooltipComponent, GridComponent])

const props = defineProps<{
    buckets: Bucket[]
    granularity: 'hour' | 'day' | 'week'
    summary: Summary | null
    totalInRange: number
    rangeLabel: string
    ms: number | null
}>()
const emit = defineEmits<{ (e: 'select', bucket: Bucket, granularity: string): void }>()
const colorMode = useColorMode()
const { t, locale } = useI18n()

const onBarClick = (params: any) => {
    const b = props.buckets[params?.dataIndex]
    if (b) emit('select', b, props.granularity)
}

const label = (b: Bucket) => {
    const d = new Date(b.bucket.length === 10 ? b.bucket + 'T00:00:00' : b.bucket + ':00')
    if (props.granularity === 'hour') return new Intl.DateTimeFormat(locale.value, { hour: '2-digit' }).format(d)
    return `${d.getMonth() + 1}/${d.getDate()}`
}

const chartOptions = computed((): EChartsOption | null => {
    const bs = props.buckets
    if (!bs.length || bs.every(b => b.total === 0)) return null
    const cats = bs.map(label)
    const ok = bs.map(b => Math.max(0, b.matched - b.matched_errors))
    const err = bs.map(b => b.matched_errors)
    const other = bs.map(b => Math.max(0, b.total - b.matched))
    const n = bs.length
    let interval = 0
    if (n > 60) interval = Math.floor(n / 12)
    else if (n > 30) interval = Math.floor(n / 10)
    else if (n > 14) interval = Math.floor(n / 8)
    const bar = { type: 'bar' as const, stack: 'runs', barWidth: '70%', barMaxWidth: 14, cursor: 'pointer' }
    return {
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            backgroundColor: 'rgba(30, 30, 30, 0.92)',
            borderColor: 'transparent',
            textStyle: { color: '#fff', fontSize: 12 },
            formatter: (params: any) => {
                if (!params?.length) return ''
                const b = bs[params[0].dataIndex]
                return `<div style="font-size:12px"><div style="font-weight:600;margin-bottom:2px">${b.bucket}</div>` +
                    `<div>${t('monitoring.diagnosis.legendMatched')}: ${b.matched.toLocaleString()} (${b.matched_errors.toLocaleString()} ${t('monitoring.diagnosis.legendErrorsShort')})</div>` +
                    `<div style="opacity:0.8">${t('monitoring.diagnosis.legendTotal')}: ${b.total.toLocaleString()}</div></div>`
            },
        },
        grid: { left: 8, right: 8, top: 6, bottom: 4, containLabel: true },
        xAxis: { type: 'category', data: cats, axisLine: { lineStyle: { color: '#e5e7eb' } }, axisTick: { show: false }, axisLabel: { interval, color: '#9ca3af', fontSize: 11 } },
        yAxis: { type: 'value', minInterval: 1, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { show: false }, splitLine: { show: false } },
        series: [
            { ...bar, name: 'ok', data: ok, itemStyle: { color: '#22c55e' } },
            { ...bar, name: 'err', data: err, itemStyle: { color: '#ef4444' } },
            { ...bar, name: 'other', data: other, itemStyle: { color: colorMode.value === 'dark' ? '#374151' : '#e5e7eb', borderRadius: [2, 2, 0, 0] } },
        ],
    }
})
</script>

<style scoped>
.chart { width: 100%; height: 100%; }
</style>
