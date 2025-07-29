<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50">
            <h3 class="text-lg font-semibold text-gray-900">Table Usage</h3>
            <p class="text-sm text-gray-500 mt-1">Most accessed data tables</p>
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
                    No table usage data available
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

// Register ECharts components
use([
    CanvasRenderer,
    BarChart,
    TitleComponent,
    TooltipComponent,
    GridComponent,
    LegendComponent,
])

interface TableUsageData {
    table_name: string
    usage_count: number
    database_name?: string
}

interface TableUsageMetrics {
    top_tables: TableUsageData[]
    total_queries_analyzed: number
}

interface Props {
    tableUsageData: TableUsageMetrics | null
    isLoading: boolean
}

const props = defineProps<Props>()

const chartOptions = computed((): EChartsOption | null => {
    if (!props.tableUsageData?.top_tables) return null
    
    // Use only top 7 tables
    const topTables = props.tableUsageData.top_tables.slice(0, 7)
    const data = topTables.map(item => item.usage_count)
    const categories = topTables.map(item => item.table_name)
    
    // Calculate max value for y-axis steps
    const maxValue = Math.max(...data)
    const stepSize = Math.ceil(maxValue / 5) // Create about 5 steps
    
    return {
        tooltip: {
            trigger: 'axis',
            axisPointer: {
                type: 'shadow'
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
            bottom: '15%',
            top: '5%',
            containLabel: true
        },
        xAxis: {
            type: 'category',
            data: categories,
            axisLine: {
                lineStyle: {
                    color: '#e0e0e0'
                }
            },
            axisTick: {
                show: false
            },
            axisLabel: {
                color: '#666',
                fontSize: 11,
                rotate: 45,
                interval: 0
            }
        },
        yAxis: {
            type: 'value',
            min: 0,
            max: Math.ceil(maxValue / stepSize) * stepSize,
            interval: stepSize, // Add fixed step intervals
            axisLine: {
                show: false
            },
            axisTick: {
                show: false
            },
            axisLabel: {
                color: '#666',
                fontSize: 12,
                formatter: (value: number) => value.toString() // Ensure integers are shown
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
                type: 'bar',
                data: data,
                barWidth: '60%',
                itemStyle: {
                    color: {
                        type: 'linear',
                        x: 0, y: 0, x2: 0, y2: 1,
                        colorStops: [
                            { offset: 0, color: '#667eea' },
                            { offset: 1, color: '#764ba2' }
                        ]
                    },
                    borderRadius: [4, 4, 0, 0]
                },
                emphasis: {
                    itemStyle: {
                        color: {
                            type: 'linear',
                            x: 0, y: 0, x2: 0, y2: 1,
                            colorStops: [
                                { offset: 0, color: '#5a6fd8' },
                                { offset: 1, color: '#6a4190' }
                            ]
                        }
                    }
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