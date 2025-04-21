<template>
    <div v-if="!isLoading && chartOptions && Object.keys(chartOptions).length > 0 && props.data?.rows?.length > 0" class="pb-5 h-full">
      <VChart class="chart" :option="chartOptions" autoresize :loading="isLoading" />
    </div>
    <div v-else-if="isLoading">
      Loading Chart...
    </div>
     <div v-else-if="!props.data?.rows?.length">
         No data to display.
    </div>
    <div v-else>
       Chart configuration error or unsupported type. Check console for details.
    </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import type { EChartsOption, SeriesOption } from 'echarts' // Import SeriesOption

// --- CORE ECHARTS IMPORTS ---
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

// --- CHART TYPE IMPORTS ---
import {
    PieChart,
    BarChart,
    LineChart,
    ScatterChart,
    HeatmapChart,
    MapChart,
    CandlestickChart,
    TreemapChart,
    RadarChart
} from 'echarts/charts'

// --- COMPONENT IMPORTS (CRITICAL FOR FEATURES) ---
import {
    TitleComponent,
    TooltipComponent,
    GridComponent,
    LegendComponent,
    ToolboxComponent,
    VisualMapComponent, 
    DataZoomComponent,  
    MarkLineComponent, 
    MarkPointComponent, 
    AriaComponent
} from 'echarts/components'

// --- REGISTER COMPONENTS ---
use([
    CanvasRenderer,
    // Charts
    PieChart,
    BarChart,
    LineChart,
    ScatterChart,
    HeatmapChart,
    MapChart,
    CandlestickChart,
    TreemapChart,
    RadarChart,
    // Components
    TitleComponent,
    TooltipComponent,
    GridComponent,
    LegendComponent,
    ToolboxComponent,
    VisualMapComponent,
    DataZoomComponent,
    MarkLineComponent,
    MarkPointComponent,
    AriaComponent
])

// --- Basic Interfaces (Refine these based on your actual data structure) ---
interface DataRow {
    [key: string]: string | number | null | undefined; // Allow null/undefined
}

interface SeriesConfig {
    name: string;
    // Common keys
    key?: string;   // For category axis (bar, line, area, scatter-x), pie name, map region, radar series name
    value?: string; // For value axis (bar, line, area), pie value, heatmap value, map value, scatter-y, treemap value
    // Specific keys (add as needed based on data_model structure)
    x?: string;         // Explicit X for scatter, heatmap-x
    y?: string;         // Explicit Y for scatter, heatmap-y
    open?: string;      // Candlestick
    close?: string;     // Candlestick
    low?: string;       // Candlestick
    high?: string;      // Candlestick
    dimensions?: string[]; // For Radar indicators/dimensions
    parentId?: string;  // For Treemap hierarchy
    id?: string;        // For Treemap hierarchy node id
    // Add other potential fields needed for future charts
}

interface DataModel {
    // Extend with new types
    type: 'pie_chart' | 'bar_chart' | 'line_chart' | 'area_chart' | 'scatter_plot' | 'heatmap' | 'map' | 'candlestick' | 'treemap' | 'radar_chart' | string;
    series: SeriesConfig[];
    // Optional: Add specific configs for complex charts
    map?: {
        mapName?: string; // e.g., 'world', 'usa'. Assumes map is registered elsewhere.
    };
    radar?: {
         indicator?: { name: string, max?: number }[]; // Explicit radar indicators
    };
    columns?: any[]; // Keep existing structure
}

interface Widget {
    title?: string;
}

interface DataProp {
    rows?: DataRow[];
}
// --- End Interfaces ---


const props = defineProps<{
    data: DataProp | null | undefined
    data_model: DataModel | null | undefined
    widget: Widget | null | undefined
    resize?: boolean
}>()

const chartOptions = ref<EChartsOption>({})
const isLoading = ref(false); // Add a loading state

// --- Helper: Base Options ---
function getBaseOptions(): EChartsOption {
    // Reset tooltip and series which are usually type-specific
    return {
        title: {
            text: props.widget?.title || 'Chart',
            left: 'center',
            top: '5'
        },
        grid: {
            containLabel: true,
            left: '3%',
            right: '4%',
            bottom: '10%',
            top: '15%' // Adjust based on title/legend/etc.
        },
        legend: { // Add basic legend
            show: false,
            orient: 'horizontal',
            left: 'center',
            top: 'bottom',
             type: 'scroll' // Allow scrolling if many items
        },
        toolbox: { // Add basic toolbox
             feature: {
                 saveAsImage: {},
                 dataZoom: { yAxisIndex: 'none' }, // Enable zoom, disable on y-axis by default
                 restore: {},
                 dataView: { readOnly: true } // Allow viewing data
             }
         },
         tooltip: { // Basic tooltip, will be overridden by specific builders
             trigger: 'item',
             confine: true
         },
         series: [] // Ensure series is empty in base, builders will provide it
    };
}

// --- Data Normalization Helper ---
function normalizeRows(rows: DataRow[] | undefined): DataRow[] {
    if (!rows) return [];
    return rows.map(row => {
        const normalizedRow: DataRow = {};
        Object.keys(row).forEach(key => {
            normalizedRow[key.toLowerCase()] = row[key];
        });
        return normalizedRow;
    });
}

// --- Helper: Get value safely ---
function getSafeValue(row: DataRow, key: string | undefined, type: 'string' | 'number' | 'any' = 'any'): string | number | null {
    if (!key) return null;
    const val = row[key.toLowerCase()];
    if (val === null || val === undefined) return null;
    if (type === 'number') {
        const num = parseFloat(String(val));
        return isNaN(num) ? null : num;
    }
    if (type === 'string') {
        return String(val);
    }
    return val; // Return as is
}


// --- Builder: Pie Chart ---
function buildPieOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    const seriesConfig = dataModel.series[0];
    if (!seriesConfig || !seriesConfig.key || !seriesConfig.value) return {};

            const seriesData = normalizedRows.map(row => ({
        name: getSafeValue(row, seriesConfig.key, 'string') ?? 'Unknown',
        value: getSafeValue(row, seriesConfig.value, 'number') ?? 0
    })).filter(item => item.name !== 'Unknown' && item.value !== null); // Filter out invalid data

            return {
        tooltip: {
            trigger: 'item',
            formatter: '{b}: {c} ({d}%)'
        },
        series: [
            {
                name: seriesConfig.name,
                type: 'pie',
                radius: ['40%', '70%'], // Example: Doughnut chart
                center: ['50%', '60%'],
                data: seriesData,
                emphasis: { /* ... */ },
                label: { /* ... */ }
            }
        ]
    };
}

// --- Builder: Cartesian Charts (Bar, Line, Area) ---
function buildCartesianOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    // Determine ECharts type and specific options
    let chartType: 'bar' | 'line';
    let specificSeriesOptions: Partial<SeriesOption> = {};
    switch (dataModel.type) {
        case 'line_chart':
            chartType = 'line';
            specificSeriesOptions = { smooth: true };
            break;
        case 'area_chart':
            chartType = 'line';
            specificSeriesOptions = { smooth: true, areaStyle: {} }; // Enable areaStyle
            break;
        case 'bar_chart':
        default:
            chartType = 'bar';
            specificSeriesOptions = { barWidth: '60%' };
            break;
    }

    const categoryKey = dataModel.series[0]?.key?.toLowerCase();
    if (!categoryKey) return {}; // Required for categories

    const categories = [...new Set(normalizedRows.map(row => getSafeValue(row, categoryKey, 'string') ?? ''))]; // Unique categories

    const series = dataModel.series.map(seriesConfig => {
        const valueKey = seriesConfig.value?.toLowerCase();
        if (!valueKey) return null; // Need a value key for each series

        // Map data ensuring alignment with unique categories
        const seriesDataMap = new Map<string, number | null>();
        normalizedRows.forEach(row => {
            const cat = getSafeValue(row, categoryKey, 'string');
            const val = getSafeValue(row, valueKey, 'number');
            if (cat !== null) {
                 seriesDataMap.set(cat, val);
            }
        });
        const seriesData = categories.map(cat => seriesDataMap.get(cat) ?? null); // Use null for missing points

            return {
                name: seriesConfig.name,
            type: chartType,
            data: seriesData,
            ...specificSeriesOptions // Apply type-specific options (smooth, areaStyle, etc.)
        };
    }).filter(Boolean) as SeriesOption[]; // Filter out nulls and assert type

    return {
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross', label: { backgroundColor: '#6a7985' } }
        },
        xAxis: {
            type: 'category',
            boundaryGap: chartType === 'bar', // Add gap for bar charts
            data: categories,
            name: dataModel.series[0]?.key || 'Categories',
            axisLabel: {
                interval: 0,
                rotate: categories.length > 10 ? 45 : 0,
                hideOverlap: true
            }
        },
        yAxis: {
            type: 'value',
            name: 'Values' // Consider making dynamic based on series names if needed
        },
        dataZoom: [
            {
                type: 'inside',
                xAxisIndex: 0,
                filterMode: 'weakFilter'
            },
            {
                show: false,
                type: 'slider',
                xAxisIndex: 0,
                start: 0,
                end: 100,
                bottom: '1%',
                height: 20,
                margin: 20
            }
        ],
        series: series
    };
}

// --- Builder: Scatter Plot ---
function buildScatterOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    // Assume first series defines X, second defines Y, could be more explicit in dataModel
    const xKey = dataModel.series[0]?.x?.toLowerCase() || dataModel.series[0]?.key?.toLowerCase();
    const yKey = dataModel.series[0]?.y?.toLowerCase() || dataModel.series[0]?.value?.toLowerCase();
    const nameKey = dataModel.series[0]?.name?.toLowerCase(); // Optional: key for individual point names

    if (!xKey || !yKey) return {}; // Need X and Y keys

    const seriesData = normalizedRows.map(row => {
        const xVal = getSafeValue(row, xKey, 'number');
        const yVal = getSafeValue(row, yKey, 'number');
        const pointName = nameKey ? getSafeValue(row, nameKey, 'string') : null;
        if (xVal === null || yVal === null) return null; // Skip points with missing coords
        return pointName ? { name: pointName, value: [xVal, yVal] } : [xVal, yVal];
    }).filter(Boolean);

    return {
        tooltip: {
            trigger: 'item', // Trigger on points
             formatter: (params: any) => { // Custom formatter
                 const data = params.value;
                 return `${params.marker}${params.seriesName}<br/>${dataModel.series[0]?.x || 'X'}: ${data[0]}<br/>${dataModel.series[0]?.y || 'Y'}: ${data[1]}`;
             }
        },
        xAxis: {
            type: 'value', // Scatter usually uses value axes
            name: dataModel.series[0]?.x || dataModel.series[0]?.key || 'X Axis',
            splitLine: { lineStyle: { type: 'dashed' } }
        },
        yAxis: {
            type: 'value',
            name: dataModel.series[0]?.y || dataModel.series[0]?.value || 'Y Axis',
            splitLine: { lineStyle: { type: 'dashed' } }
        },
        series: [
            {
                name: dataModel.series[0]?.name || 'Scatter Data',
                type: 'scatter',
                symbolSize: 10, // Adjust point size
                data: seriesData
            }
        ]
    };
}

// --- Builder: Heatmap ---
function buildHeatmapOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    // Requires xKey, yKey, valueKey from dataModel.series (assuming first series config)

    debugger
    const config = dataModel.series[0];
    const xKey = config?.x?.toLowerCase() || config?.key?.toLowerCase(); // Or some convention
    const yKey = config?.y?.toLowerCase(); // Need a separate key for Y
    const valueKey = config?.value?.toLowerCase();
    if (!xKey || !yKey || !valueKey) return {}; // Essential keys missing

    const xCategories = [...new Set(normalizedRows.map(row => getSafeValue(row, xKey, 'string')))].filter(Boolean) as string[];
    const yCategories = [...new Set(normalizedRows.map(row => getSafeValue(row, yKey, 'string')))].filter(Boolean) as string[];

    debugger
    const seriesData = normalizedRows.map(row => {
        const xVal = getSafeValue(row, xKey, 'string');
        const yVal = getSafeValue(row, yKey, 'string');
        const heatVal = getSafeValue(row, valueKey, 'number');

        const xIndex = xCategories.indexOf(xVal ?? '');
        const yIndex = yCategories.indexOf(yVal ?? '');

        if (xIndex === -1 || yIndex === -1 || heatVal === null) return null; // Skip invalid data
        return [xIndex, yIndex, heatVal];
    }).filter(Boolean);

     const maxHeat = Math.max(...seriesData.map(d => d?.[2] ?? 0).filter(v => v !== null) as number[]);

    return {
        tooltip: {
            position: 'top'
        },
        grid: { // Adjust grid for heatmap labels
            height: '60%',
            top: '10%',
             bottom: '20%'
        },
        xAxis: {
            type: 'category',
            data: xCategories,
            splitArea: { show: true },
             axisLabel: { interval: 0, rotate: xCategories.length > 10 ? 45 : 0 }
        },
        yAxis: {
            type: 'category',
            data: yCategories,
            splitArea: { show: true },
             axisLabel: { interval: 0 }
        },
        visualMap: { // Essential for heatmap coloring
            min: 0,
            max: maxHeat,
            calculable: true,
            orient: 'horizontal',
            left: 'center',
            bottom: '5%'
        },
        series: [
            {
                name: config?.name || 'Heatmap Data',
                type: 'heatmap',
                data: seriesData,
                label: { show: true }, // Show values on squares
                emphasis: {
                    itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0, 0, 0, 0.5)' }
                }
            }
        ]
    };
}

// --- Builder: Map ---
function buildMapOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    // Assumes map (e.g., 'world') is registered globally
    const mapName = dataModel.map?.mapName || 'world'; // Get map name from model or default
    const config = dataModel.series[0];
    const regionKey = config?.key?.toLowerCase(); // Key for region names (e.g., country name)
    const valueKey = config?.value?.toLowerCase(); // Key for the value associated with the region

    if (!regionKey || !valueKey) return {};

    const seriesData = normalizedRows.map(row => ({
        name: getSafeValue(row, regionKey, 'string') ?? 'Unknown',
        value: getSafeValue(row, valueKey, 'number') ?? 0
    })).filter(item => item.name !== 'Unknown' && item.value !== null);

    const maxVal = Math.max(...seriesData.map(d => d.value).filter(v => v !== null) as number[], 0);

    return {
         tooltip: {
             trigger: 'item',
             formatter: '{b}: {c}' // Simple formatter: Region: Value
         },
         visualMap: { // Color mapping based on value
             left: 'right',
             min: 0,
             max: maxVal,
             inRange: { // Example color range
                 color: ['#e0f3f8', '#abd9e9', '#74add1', '#4575b4', '#313695']
             },
             calculable: true
         },
         series: [
             {
                 name: config?.name || mapName, // Use series name or map name
                 type: 'map',
                 map: mapName, // Specify the registered map name
                 roam: true, // Allow zooming and panning
                 emphasis: { label: { show: true } },
                 data: seriesData
             }
         ]
    };
}


// --- Builder: Candlestick ---
function buildCandlestickOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    // Requires category/time key, open, close, low, high keys
    const config = dataModel.series[0];
    const categoryKey = config?.key?.toLowerCase(); // Date or category
    const openKey = config?.open?.toLowerCase();
    const closeKey = config?.close?.toLowerCase();
    const lowKey = config?.low?.toLowerCase();
    const highKey = config?.high?.toLowerCase();

    if (!categoryKey || !openKey || !closeKey || !lowKey || !highKey) return {};

    const categories: string[] = [];
    const seriesData: (number | null)[][] = [];

    normalizedRows.forEach(row => {
        const cat = getSafeValue(row, categoryKey, 'string');
        const openVal = getSafeValue(row, openKey, 'number');
        const closeVal = getSafeValue(row, closeKey, 'number');
        const lowVal = getSafeValue(row, lowKey, 'number');
        const highVal = getSafeValue(row, highKey, 'number');

        if (cat !== null && openVal !== null && closeVal !== null && lowVal !== null && highVal !== null) {
             categories.push(cat);
             seriesData.push([openVal, closeVal, lowVal, highVal]);
        }
    });

    return {
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross' }
        },
        xAxis: {
            type: 'category',
            data: categories,
             axisLabel: { interval: 0, rotate: categories.length > 10 ? 45 : 0 }
        },
        yAxis: {
            type: 'value',
            scale: true, // Allow scaling to fit data
            splitArea: { show: true }
        },
        dataZoom: [
            {
                type: 'inside',
                xAxisIndex: 0,
                filterMode: 'weakFilter'
            },
            {
                show: true,
                type: 'slider',
                xAxisIndex: 0,
                start: 0,
                end: 100,
                bottom: '1%',
                height: 20
            }
        ],
        series: [
            {
                name: config?.name || 'Candlestick',
                type: 'candlestick',
                data: seriesData,
                 itemStyle: { // Example styling
                     color: '#ef232a', // Down color
                     color0: '#14b143', // Up color
                     borderColor: '#ef232a',
                     borderColor0: '#14b143'
                 }
            }
        ]
    };
}

// --- Builder: Treemap ---
function buildTreemapOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    // Requires keys for id, parentId, value, name
    const config = dataModel.series[0];
    const idKey = config?.id?.toLowerCase() || 'id'; // Default convention
    const parentIdKey = config?.parentId?.toLowerCase() || 'parentid';
    const valueKey = config?.value?.toLowerCase();
    const nameKey = config?.key?.toLowerCase() || config?.name?.toLowerCase() || 'name'; // Use key or name

    if (!valueKey || !nameKey) return {}; // Need at least name and value

    // Simple flat-to-hierarchical conversion (assumes root nodes have null/undefined parentId)
    // More complex hierarchy building might be needed depending on data source
    const buildHierarchy = (rows: DataRow[]): any[] => {
        const map = new Map<string | number, any>();
        const tree: any[] = [];

        rows.forEach(row => {
            const id = getSafeValue(row, idKey, 'any');
            const parentId = getSafeValue(row, parentIdKey, 'any');
            const value = getSafeValue(row, valueKey, 'number');
            const name = getSafeValue(row, nameKey, 'string');

            if (value === null || name === null || id === null) return; // Skip invalid nodes

            map.set(id, {
                id: id,
                name: name,
                value: value,
                children: []
            });
        });

        map.forEach(node => {
            const parentId = getSafeValue(normalizedRows.find(r => getSafeValue(r, idKey, 'any') === node.id)!, parentIdKey, 'any');
            if (parentId !== null && map.has(parentId)) {
                map.get(parentId).children.push(node);
            } else {
                tree.push(node); // Add to root if no parent or parent not found
            }
        });
        return tree;
    };

    const treeData = buildHierarchy(normalizedRows);

    return {
        tooltip: { // Tooltip for treemap
             formatter: '{b}: {c}'
        },
        series: [
            {
                name: config?.name || 'Treemap',
                type: 'treemap',
                visibleMin: 300, // Don't display nodes smaller than 300 pixels
                label: { show: true, formatter: '{b}' },
                itemStyle: { borderColor: '#fff' },
                levels: [ // Define level-specific styles if needed
                     { itemStyle: { borderColor: '#777', borderWidth: 0, gapWidth: 1 } },
                     { itemStyle: { borderColor: '#555', gapWidth: 1 } }
                 ],
                breadcrumb: { // Show navigation path
                    show: true,
                     left: 'center',
                     top: '10%'
                },
                data: treeData
            }
        ]
    };
}

// --- Builder: Radar Chart ---
function buildRadarOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    // Requires indicator definitions and series data
    // Indicators might come from dataModel.radar.indicator OR inferred from dataModel.series.dimensions
    // Series data structure: [{ name: 'SeriesA', value: [1,2,3] }, { name: 'SeriesB', value: [4,5,6] }]

    let indicators: { name: string, max?: number }[] = [];
    if (dataModel.radar?.indicator) {
        indicators = dataModel.radar.indicator;
    } else if (dataModel.series[0]?.dimensions?.length) {
        // Infer indicators from first series dimensions, assume max based on data
        const dimKeys = dataModel.series[0].dimensions.map(d => d.toLowerCase());
        const maxValues: { [key: string]: number } = {};
        dimKeys.forEach(key => maxValues[key] = 0);

        normalizedRows.forEach(row => {
            dimKeys.forEach(key => {
                const val = getSafeValue(row, key, 'number');
                if (val !== null && val > maxValues[key]) {
                    maxValues[key] = val;
                }
            });
        });
        indicators = dimKeys.map(key => ({ name: dataModel.series[0].dimensions?.find(d => d.toLowerCase() === key) || key, max: maxValues[key] * 1.1 })); // Add 10% buffer to max
    } else {
        return {}; // Cannot build radar without indicators
    }

    // Series data: Assume each row is a data point for *one* series, identified by 'key'
    // OR assume each series config in dataModel.series defines a radar series
    const seriesDataMap = new Map<string, number[]>();
    const seriesNames: string[] = [];

    dataModel.series.forEach(seriesConfig => {
         const seriesName = seriesConfig.name;
         const dimKeys = seriesConfig.dimensions?.map(d => d.toLowerCase()) || indicators.map(ind => ind.name.toLowerCase()); // Use defined dims or all indicators
         const seriesValues: number[] = [];

         // Find the row matching this series (if data is structured one row per series)
         // This part is highly dependent on the incoming data structure.
         // Let's assume each row is a full dataset for one series defined by 'key'
         const seriesRow = normalizedRows.find(row => getSafeValue(row, seriesConfig.key || 'name', 'string') === seriesName);

         if (seriesRow) {
             dimKeys.forEach(key => {
                 seriesValues.push(getSafeValue(seriesRow, key, 'number') ?? 0);
             });
             seriesDataMap.set(seriesName, seriesValues);
             seriesNames.push(seriesName);
         }
         // ALT: If each row is a *point* and seriesConfig.value defines the value column:
         // This would require grouping by seriesConfig.name/key first.
    });


    const series = [...seriesDataMap.entries()].map(([name, values]) => ({
        name: name,
        value: values
    }));


    return {
        tooltip: { trigger: 'item' }, // Trigger on series points
        legend: {
             data: seriesNames, // Use the collected series names for the legend
             bottom: '1%'
         },
        radar: {
            indicator: indicators,
             shape: 'circle', // 'polygon' or 'circle'
             center: ['50%', '55%'], // Adjust position
             radius: '65%'
        },
        series: [
            {
                name: dataModel.widget?.title || 'Radar Data', // Overall name for the chart
                type: 'radar',
                data: series // Array of { name: string, value: number[] }
            }
        ]
    };
}


// --- Main Dispatcher Function ---
async function buildChartOptions() {
    isLoading.value = true;
    chartOptions.value = {}; // Clear previous options

    // Guard clauses
    if (!props.data_model || !props.data?.rows?.length) {
        console.warn("Missing data model or data rows.");
        isLoading.value = false;
        return;
    }

    const chartType = props.data_model.type;
    const normalizedRows = normalizeRows(props.data.rows);

    let specificOptions: EChartsOption = {};
    const baseOptions = getBaseOptions();

    // --- Dispatch Logic ---
    try {

        switch (chartType) {
            case 'pie_chart':
                specificOptions = buildPieOptions(normalizedRows, props.data_model);
                break;
            case 'bar_chart':
            case 'line_chart':
            case 'area_chart': // Added area_chart here
                specificOptions = buildCartesianOptions(normalizedRows, props.data_model);
                break;
             case 'scatter_plot':
                 specificOptions = buildScatterOptions(normalizedRows, props.data_model);
                 break;
             case 'heatmap':
                 specificOptions = buildHeatmapOptions(normalizedRows, props.data_model);
                 break;
             case 'map':
                 specificOptions = buildMapOptions(normalizedRows, props.data_model);
                 break;
             case 'candlestick':
                 specificOptions = buildCandlestickOptions(normalizedRows, props.data_model);
                 break;
             case 'treemap':
                 specificOptions = buildTreemapOptions(normalizedRows, props.data_model);
                 break;
             case 'radar_chart':
                 specificOptions = buildRadarOptions(normalizedRows, props.data_model);
                 break;
            default:
                console.warn(`Unsupported chart type: ${chartType}`);
                specificOptions = { title: { ...baseOptions.title, text: 'Unsupported Chart Type' } };
                break;
        }

        // Simple merge - prioritize specific options over base for keys present in both
        // Note: This is a shallow merge. Deeper merging might be needed for complex cases (e.g., merging tooltip formatters).
        chartOptions.value = { ...baseOptions, ...specificOptions };

        // Example of more specific merging if needed:
        // chartOptions.value = {
        //     ...baseOptions,
        //     ...specificOptions,
        //     tooltip: { ...baseOptions.tooltip, ...specificOptions.tooltip }, // Explicitly merge tooltip
        //     // visualMap usually replaces, not merges
        //     // series usually replaces
        // };


    } catch (error) {
        console.error(`Error building options for ${chartType}:`, error);
        chartOptions.value = { title: { text: 'Error Building Chart' } }; // Display error state
    } finally {
         // Add a small delay to prevent flash of loading state if processing is very fast
        await new Promise(resolve => setTimeout(resolve, 50));
        isLoading.value = false;
    }
}

// Watch triggers the dispatcher
watch([() => props.data?.rows, () => props.data_model], () => {
    buildChartOptions();
}, { immediate: true, deep: true }); // Deep watch on data_model is important if its structure changes

</script>

<style scoped>
.chart {
    width: 100%;
    min-height: 100px;
    height: 100%;
}
/* Add styles for loading/error states if desired */
</style>