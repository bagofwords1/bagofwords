<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50">
            <h3 class="text-lg font-semibold text-gray-900">Top Prompt Types</h3>
            <p class="text-sm text-gray-500 mt-1">Most popular prompt categories</p>
        </div>
        <div class="p-6">
            <div class="h-80">
                <VChart
                    v-if="chartOptions"
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

interface PromptTypeData {
    type: string
    count: number
}

interface Props {
    promptTypesData: PromptTypeData[]
}

const props = defineProps<Props>()

const chartOptions = computed((): EChartsOption => {
    const data = props.promptTypesData.map(item => item.count)
    const categories = props.promptTypesData.map(item => item.type)
    
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
            left: '0%',  // More space for category labels
            right: '4%',
            bottom: '10%',
            top: '5%',
            containLabel: true
        },
        // Swap x and y axes for horizontal bars
        xAxis: {
            type: 'value',  // Values on x-axis now
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
        yAxis: {
            type: 'category',  // Categories on y-axis now
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
                interval: 0  // Show all labels
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
                        x: 0, y: 0, x2: 1, y2: 0,  // Horizontal gradient
                        colorStops: [
                            { offset: 0, color: '#667eea' },
                            { offset: 1, color: '#764ba2' }
                        ]
                    },
                    borderRadius: [0, 4, 4, 0]  // Rounded on the right side
                },
                emphasis: {
                    itemStyle: {
                        color: {
                            type: 'linear',
                            x: 0, y: 0, x2: 1, y2: 0,
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