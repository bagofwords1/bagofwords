<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50">
            <h3 class="text-lg font-semibold text-gray-900">Activity Over Time</h3>
            <p class="text-sm text-gray-500 mt-1">Messages and queries trends</p>
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
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import {
    TitleComponent,
    TooltipComponent,
    GridComponent,
    LegendComponent,
} from 'echarts/components'
import type { EChartsOption } from 'echarts'

// Register ECharts components
use([
    CanvasRenderer,
    LineChart,
    TitleComponent,
    TooltipComponent,
    GridComponent,
    LegendComponent,
])

interface TimeSeriesPoint {
    date: string
    value: number
}

interface ActivityMetrics {
    messages: TimeSeriesPoint[]
    queries: TimeSeriesPoint[]
}

interface Props {
    activityMetrics: ActivityMetrics | null
    isLoading: boolean
}

const props = defineProps<Props>()

const chartOptions = computed((): EChartsOption | null => {
    if (!props.activityMetrics) return null
    
    const messages = props.activityMetrics.messages
    const queries = props.activityMetrics.queries
    
    const dates = messages.map(item => {
        const date = new Date(item.date)
        return `${date.getMonth() + 1}/${date.getDate()}`
    })
    
    const messagesData = messages.map(item => item.value)
    const queriesData = queries.map(item => item.value)
    
    const numDates = dates.length
    let interval = 0
    if (numDates > 20) {
        interval = Math.floor(numDates / 8)
    } else if (numDates > 10) {
        interval = Math.floor(numDates / 6)
    }
    
    return {
        tooltip: {
            trigger: 'axis',
            axisPointer: {
                type: 'line',
                lineStyle: {
                    color: '#999',
                    width: 1,
                    type: 'dashed'
                }
            },
            backgroundColor: 'rgba(50, 50, 50, 0.9)',
            borderColor: 'transparent',
            textStyle: {
                color: '#fff',
                fontSize: 12
            }
        },
        grid: {
            left: '3%',
            right: '4%',
            bottom: '10%',
            top: '5%',
            containLabel: true,
            show: true,
            borderColor: '#f0f0f0'
        },
        xAxis: {
            type: 'category',
            data: dates,
            axisLine: {
                lineStyle: {
                    color: '#e0e0e0'
                }
            },
            axisTick: {
                show: false
            },
            axisLabel: {
                interval: interval,
                color: '#666',
                fontSize: 12
            },
            splitLine: {
                show: false
            }
        },
        yAxis: {
            type: 'value',
            axisLine: {
                show: false
            },
            axisTick: {
                show: false
            },
            axisLabel: {
                color: '#666',
                fontSize: 12
            },
            splitLine: {
                show: true,
                lineStyle: {
                    color: '#f0f0f0',
                    type: 'dashed'
                }
            }
        },
        series: [
            {
                name: 'Messages',
                type: 'line',
                data: messagesData,
                smooth: true,
                showSymbol: false,
                areaStyle: {
                    opacity: 0.3,
                    color: {
                        type: 'linear',
                        x: 0, y: 0, x2: 0, y2: 1,
                        colorStops: [
                            { offset: 0, color: 'rgba(102, 126, 234, 0.3)' },
                            { offset: 1, color: 'rgba(102, 126, 234, 0.1)' }
                        ]
                    }
                },
                lineStyle: {
                    width: 2,
                    color: '#667eea'
                },
                itemStyle: {
                    color: '#667eea'
                }
            },
            {
                name: 'Queries',
                type: 'line',
                data: queriesData,
                smooth: true,
                showSymbol: false,
                areaStyle: {
                    opacity: 0.3,
                    color: {
                        type: 'linear',
                        x: 0, y: 0, x2: 0, y2: 1,
                        colorStops: [
                            { offset: 0, color: 'rgba(118, 75, 162, 0.3)' },
                            { offset: 1, color: 'rgba(118, 75, 162, 0.1)' }
                        ]
                    }
                },
                lineStyle: {
                    width: 2,
                    color: '#764ba2'
                },
                itemStyle: {
                    color: '#764ba2'
                }
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