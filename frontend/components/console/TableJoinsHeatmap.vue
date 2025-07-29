<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50">
            <h3 class="text-lg font-semibold text-gray-900">Table Joins Heatmap</h3>
            <p class="text-sm text-gray-500 mt-1">Table relationship patterns</p>
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
                    No table joins data available
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { HeatmapChart } from 'echarts/charts'
import {
    TitleComponent,
    TooltipComponent,
    GridComponent,
    VisualMapComponent,
} from 'echarts/components'
import type { EChartsOption } from 'echarts'

// Register ECharts components
use([
    CanvasRenderer,
    HeatmapChart,
    TitleComponent,
    TooltipComponent,
    GridComponent,
    VisualMapComponent,
])

interface TableJoinData {
    table1: string
    table2: string
    join_count: number
}

interface TableJoinsHeatmap {
    table_pairs: TableJoinData[]
    unique_tables: string[]
    total_queries_analyzed: number
}

interface Props {
    tableJoinsData: TableJoinsHeatmap | null
    isLoading: boolean
}

const props = defineProps<Props>()

const chartOptions = computed((): EChartsOption | null => {
    if (!props.tableJoinsData?.table_pairs || !props.tableJoinsData?.unique_tables) return null
    
    const tables = props.tableJoinsData.unique_tables.slice(0, 6) // Limit to top 8 tables for readability
    const pairs = props.tableJoinsData.table_pairs
    
    // Create a matrix for the heatmap
    const data = []
    const maxJoinCount = Math.max(...pairs.map(pair => pair.join_count))
    
    for (let i = 0; i < tables.length; i++) {
        for (let j = 0; j < tables.length; j++) {
            if (i !== j) {
                const pair = pairs.find(p => 
                    (p.table1 === tables[i] && p.table2 === tables[j]) ||
                    (p.table1 === tables[j] && p.table2 === tables[i])
                )
                data.push([j, i, pair ? pair.join_count : 0])
            } else {
                data.push([j, i, 0]) // No self-joins
            }
        }
    }
    
    return {
        tooltip: {
            position: 'top',
            backgroundColor: 'rgba(50, 50, 50, 0.9)',
            borderColor: 'transparent',
            textStyle: {
                color: '#fff',
                fontSize: 12
            },
            formatter: (params: any) => {
                const table1 = tables[params.data[1]]
                const table2 = tables[params.data[0]]
                const joins = params.data[2]
                return `<div style="font-weight: 600;">${table1} ↔ ${table2}</div>
                        <div>${joins} joins</div>`
            }
        },
        grid: {
            width: '70%',
            top: '0',
            bottom: '35%',
            left: '30%',
            right: '5%'
        },
        xAxis: {
            type: 'category',
            data: tables,
            splitArea: {
                show: true
            },
            axisLabel: {
                color: '#666',
                fontSize: 12,
                rotate: 45
            },
            axisLine: {
                show: false
            },
            axisTick: {
                show: false
            }
        },
        yAxis: {
            type: 'category',
            data: tables,
            splitArea: {
                show: true
            },
            axisLabel: {
                color: '#666',
                fontSize: 12
            },
            axisLine: {
                show: false
            },
            axisTick: {
                show: false
            }
        },
        visualMap: {
            show: false,
            min: 0,
            max: maxJoinCount,
            calculable: true,
            orient: 'horizontal',
            left: 'center',
            bottom: '5%',
            inRange: {
                color: ['#e6f3ff', '#4ade80', '#22c55e', '#16a34a', '#15803d']
            }
        },
        series: [
            {
                type: 'heatmap',
                data: data,
                label: {
                    show: true,
                    color: '#fff',
                    fontSize: 11,
                    fontWeight: 'bold'
                },
                emphasis: {
                    itemStyle: {
                        shadowBlur: 10,
                        shadowColor: 'rgba(0, 0, 0, 0.5)'
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