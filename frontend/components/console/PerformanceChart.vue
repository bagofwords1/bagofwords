<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50">
            <h3 class="text-lg font-semibold text-gray-900">Performance Metrics</h3>
            <p class="text-sm text-gray-500 mt-1">Accuracy and efficiency trends</p>
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

interface TimeSeriesPointFloat {
    date: string
    value: number
}

interface PerformanceMetrics {
    accuracy: TimeSeriesPointFloat[]
    instructions_coverage: TimeSeriesPointFloat[]
    instructions_effectiveness: TimeSeriesPointFloat[]
    context_effectiveness: TimeSeriesPointFloat[]
    response_quality: TimeSeriesPointFloat[]
    positive_feedback_rate: TimeSeriesPointFloat[]
}

interface Props {
    performanceMetrics: PerformanceMetrics | null
    isLoading: boolean
}

const props = defineProps<Props>()

const chartOptions = computed((): EChartsOption | null => {
    if (!props.performanceMetrics) return null
    
    const accuracy = props.performanceMetrics.accuracy
    const instructionsEffectiveness = props.performanceMetrics.instructions_effectiveness || []
    
    const dates = accuracy.map(item => {
        const date = new Date(item.date)
        return `${date.getMonth() + 1}/${date.getDate()}`
    })
    
    const accuracyData = accuracy.map(item => item.value)
    const instructionsEffectivenessData = instructionsEffectiveness.map(item => item.value)
    
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
            },
            formatter: (params: any) => {
                let tooltip = `<div style="font-weight: 600; margin-bottom: 4px;">${params[0].axisValue}</div>`
                params.forEach((param: any) => {
                    const value = param.value
                    const formattedValue = param.seriesName === 'Accuracy' ? `${value.toFixed(1)}%` : `${value.toFixed(1)}`
                    tooltip += `<div style="display: flex; align-items: center; margin-bottom: 2px;">
                        <span style="display: inline-block; width: 10px; height: 10px; background-color: ${param.color}; border-radius: 50%; margin-right: 8px;"></span>
                        <span style="margin-right: 8px;">${param.seriesName}:</span>
                        <span style="font-weight: 600;">${formattedValue}</span>
                    </div>`
                })
                return tooltip
            }
        },
        grid: {
            left: '3%',
            right: '4%',
            bottom: '15%',
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
            min: 0,
            max: 100,
            axisLine: {
                show: false
            },
            axisTick: {
                show: false
            },
            axisLabel: {
                color: '#666',
                fontSize: 12,
                formatter: (value: number) => {
                    // For accuracy (percentage), show with %
                    // For judge scores (0-100), show as score
                    return value.toFixed(0)
                }
            },
            splitLine: {
                show: true,
                lineStyle: {
                    color: '#f0f0f0',
                    type: 'dashed'
                }
            }
        },
        legend: {
            show: true,
            bottom: 10,
            textStyle: {
                fontSize: 12,
                color: '#666'
            },
            icon: 'circle',
            itemWidth: 8,
            itemHeight: 8
        },
        series: [
            {
                name: 'Accuracy',
                type: 'line',
                data: accuracyData,
                smooth: true,
                showSymbol: false,
                lineStyle: {
                    width: 3,
                    color: '#4ade80'
                },
                itemStyle: {
                    color: '#4ade80'
                }
            },
            {
                name: 'Instructions Effectiveness',
                type: 'line',
                data: instructionsEffectivenessData,
                smooth: true,
                showSymbol: false,
                lineStyle: {
                    width: 3,
                    color: '#3b82f6'
                },
                itemStyle: {
                    color: '#3b82f6'
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