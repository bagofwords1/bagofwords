<template>
    <div v-if="data.rows?.length > 0" class="pb-5">
        <VChart class="chart" :option="chartOptions" autoresize />
    </div>
    <div v-else>
        Loading...
    </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{
    data: any
    data_model: any
    widget: any
    resize: boolean
}>()

const chartOptions = ref({})



function buildChartOptions() {
    if (!props.data_model || !props.data.rows?.length) return;

    // Create a normalized version of the rows with lowercase keys
    const normalizedRows = props.data.rows.map(row => {
        const normalizedRow = {};
        Object.keys(row).forEach(key => {
            normalizedRow[key.toLowerCase()] = row[key];
        });
        return normalizedRow;
    });

    const chartType = props.data_model.type;
    const mappedType = ref('');

    // Map the chart type to ECharts types
    if (chartType === 'pie_chart') {
        mappedType.value = 'pie';
    } else if (chartType === 'bar_chart') {
        mappedType.value = 'bar';
    } else if (chartType === 'line_chart') {
        mappedType.value = 'line';
    }

    // Build series based on type
    const series = props.data_model.series.map(seriesConfig => {
        if (chartType === 'pie_chart') {
            const seriesData = normalizedRows.map(row => ({
                name: row[seriesConfig.key.toLowerCase()],
                value: parseFloat(row[seriesConfig.value.toLowerCase()])
            }));

            return {
                name: seriesConfig.name,
                type: 'pie',
                data: seriesData,
                radius: '50%',
                label: {
                    show: true,
                    formatter: '{b}: {c}'
                }
            };
        } else {
            const seriesData = normalizedRows.map(row => parseFloat(row[seriesConfig.value.toLowerCase()]));

            return {
                name: seriesConfig.name,
                type: mappedType.value,
                data: seriesData
            };
        }
    }).filter(Boolean);

    // Get categories for x-axis (only for bar/line charts)
    const categories = ['bar_chart', 'line_chart'].includes(chartType)
        ? normalizedRows.map(row => row[props.data_model.series[0].key.toLowerCase()])
        : undefined;

    // Build chart options for ECharts
    chartOptions.value = {
        title: {
            text: props.widget.title || 'Chart'
        },
        tooltip: {
            trigger: chartType === 'pie_chart' ? 'item' : 'axis',
            formatter: chartType === 'pie_chart' 
                ? '{b}: {c}'
                : '{b}: {c}'
        },
        xAxis: categories ? {
            type: 'category',
            data: categories,
            name: props.data_model.series[0]?.key || 'Categories',
            axisLabel: {
                interval: 0,  
                rotate: 45    
            }
        } : undefined,
        yAxis: categories ? {
            type: 'value',
            name: 'Values'
        } : undefined,
        series: series
    };
}

watch([() => props.data?.rows, () => props.data_model?.type], () => {
    buildChartOptions()
}, { immediate: true, deep: true })
</script>

<style scoped>
.chart {
    width: 100%;
    min-height: 450px;  /* Add a default height */
}
</style>