<template>
    <div v-if="!isLoading && chartOptions && Object.keys(chartOptions).length > 0 && props.data?.rows?.length > 0" class="h-full">
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

    // {{ Updated Dynamic Interval Logic }}
    const numCategories = categories.length;
    let labelInterval = 0;
    let labelRotate = 0;
    let hideOverlap = false; // Default to not hiding for low counts

    if (numCategories > 50) { // Very High density
        labelInterval = Math.max(1, Math.floor(numCategories / 20));
        labelRotate = 45;
        hideOverlap = true;
    } else if (numCategories > 25) { // High density
        labelInterval = 1; // Skip every other
        labelRotate = 45;
        hideOverlap = true;
    } else if (numCategories > 10) { // Medium-High density (Rotate and skip)
        labelInterval = 1; // Start skipping (every other)
        labelRotate = 45;
        hideOverlap = true;
    } else if (numCategories > 5) { // Medium density (Rotate only)
        labelInterval = 0; // Show all
        labelRotate = 45;
        hideOverlap = true; // Hide if they still overlap after rotation
    } else { // Low density (<= 5): Show all, no rotation
        labelInterval = 0;
        labelRotate = 0;
        hideOverlap = false; // Try showing all even if slight overlap
    }
    // {{ End Updated Dynamic Interval Logic }}

    const series = dataModel.series.map(seriesConfig => {
        const valueKey = seriesConfig.value?.toLowerCase();
        if (!valueKey) return null;

        // Map data ensuring alignment with unique categories
        const seriesDataMap = new Map<string, number | null>();
        normalizedRows.forEach(row => {
            const cat = getSafeValue(row, categoryKey, 'string');
            const val = getSafeValue(row, valueKey, 'number');
            if (cat !== null) {
                 seriesDataMap.set(cat, val);
            }
        });
        const seriesData = categories.map(cat => seriesDataMap.get(cat) ?? null);

        return {
            name: seriesConfig.name,
            type: chartType,
            data: seriesData,
            ...specificSeriesOptions
        };
    }).filter(Boolean) as SeriesOption[];

    // {{ Calculate required bottom padding }}
    let gridBottomPadding = 12; // Base percentage
    if (labelRotate > 0) {
        gridBottomPadding = Math.max(gridBottomPadding, 18);
    }
    gridBottomPadding = Math.max(gridBottomPadding, 12); // Space for potential slider

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
                interval: labelInterval,
                rotate: labelRotate,
                hideOverlap: hideOverlap
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
    const config = dataModel.series[0];
    const xKey = config?.x?.toLowerCase() || config?.key?.toLowerCase();
    const yKey = config?.y?.toLowerCase();
    const valueKey = config?.value?.toLowerCase();
    if (!xKey || !yKey || !valueKey) return {};

    const xCategories = [...new Set(normalizedRows.map(row => getSafeValue(row, xKey, 'string')))].filter(Boolean) as string[];
    const yCategories = [...new Set(normalizedRows.map(row => getSafeValue(row, yKey, 'string')))].filter(Boolean) as string[];

    // {{ Updated Dynamic Interval Logic for X-Axis }}
    const numXCategories = xCategories.length;
    let xLabelInterval = 0;
    let xLabelRotate = 0;
    let xHideOverlap = false; // Default to not hiding for low counts

    if (numXCategories > 50) { // Very High density
        xLabelInterval = Math.max(1, Math.floor(numXCategories / 20));
        xLabelRotate = 45;
        xHideOverlap = true;
    } else if (numXCategories > 25) { // High density
        xLabelInterval = 1;
        xLabelRotate = 45;
        xHideOverlap = true;
    } else if (numXCategories > 10) { // Medium-High density (Rotate and skip)
        xLabelInterval = 1;
        xLabelRotate = 45;
        xHideOverlap = true;
    } else if (numXCategories > 5) { // Medium density (Rotate only)
        xLabelInterval = 0;
        xLabelRotate = 45;
        xHideOverlap = true;
    } else { // Low density (<= 5): Show all, no rotation
        xLabelInterval = 0;
        xLabelRotate = 0;
        xHideOverlap = false;
    }
    // {{ End Updated Dynamic Interval Logic for X-Axis }}

    // {{ Dynamic Interval Logic for Y-Axis (Keep simpler logic, no rotation) }}
    const numYCategories = yCategories.length;
    let yLabelInterval = 0;
    let yHideOverlap = true; // Default hide

    if (numYCategories > 50) {
        yLabelInterval = Math.max(1, Math.floor(numYCategories / 20));
    } else if (numYCategories > 25) {
        yLabelInterval = 1;
    } else if (numYCategories > 10) { // Start skipping earlier for Y maybe
        yLabelInterval = 1;
    }
     else { // Low/Medium density <= 10
        yLabelInterval = 0;
        yHideOverlap = false; // Try to show all Y if few enough
    }
    // {{ End Dynamic Interval Logic for Y-Axis }}


    const seriesData = normalizedRows.map(row => {
        const xVal = getSafeValue(row, xKey, 'string');
        const yVal = getSafeValue(row, yKey, 'string');
        const heatVal = getSafeValue(row, valueKey, 'number');

        const xIndex = xCategories.indexOf(xVal ?? '');
        const yIndex = yCategories.indexOf(yVal ?? '');

        if (xIndex === -1 || yIndex === -1 || heatVal === null) return null;
        // Store original values along with indices for tooltip
        return {
            value: [xIndex, yIndex, heatVal],
            originalX: xVal,
            originalY: yVal
        };
    }).filter(item => item !== null); // Filter out null items

    const maxHeat = Math.max(...seriesData.map(d => d?.value[2] ?? 0).filter(v => v !== null) as number[], 0); // Adjust maxHeat calculation

    // {{ Calculate grid padding }}
    let gridBottomPadding = xLabelRotate > 0 ? 25 : 20; // Base for heatmap visualMap
    let gridLeftPadding = 10; // Base for Y axis
    // Potentially increase left padding if Y labels are very long (harder to calculate dynamically)
    // gridLeftPadding = Math.max(10, longestYLabelLength * estimatedCharWidth / totalWidth);

    return {
        tooltip: {
            position: 'top',
            formatter: (params: any) => {
                // params.data should contain the object we created in seriesData mapping
                const dataItem = params.data;
                if (dataItem && dataItem.value && dataItem.value.length === 3) {
                    const xIndex = dataItem.value[0];
                    const yIndex = dataItem.value[1];
                    const heatValue = dataItem.value[2];

                    // Get original category names using indices
                    const xCatName = xCategories[xIndex] || 'N/A';
                    const yCatName = yCategories[yIndex] || 'N/A';

                    // Use original values if stored, otherwise fallback to index lookup
                    const xOriginal = dataItem.originalX || xCatName;
                    const yOriginal = dataItem.originalY || yCatName;

                    // Construct the tooltip string
                    // Assuming X = Customer, Y = Film, Value = Rentals based on screenshot
                    // Adjust field names as needed
                    return `
                        ${params.marker} <b>${config.value || 'Value'}</b>: ${heatValue}<br/>
                        <b>${config.x || config.key || 'X'}</b>: ${xOriginal}<br/>
                        <b>${config.y || 'Y'}</b>: ${yOriginal}
                    `;
                }
                return ''; // Return empty string if data format is unexpected
            }
        },
        grid: {
            height: '60%',
            top: '10%',
            bottom: `${gridBottomPadding}%`,
            left: `${gridLeftPadding}%`,
            containLabel: true
        },
        xAxis: {
            type: 'category',
            data: xCategories,
            splitArea: { show: true },
             axisLabel: {
                 interval: xLabelInterval,
                 rotate: xLabelRotate,
                 hideOverlap: xHideOverlap
             }
        },
        yAxis: {
            type: 'category',
            data: yCategories,
            splitArea: { show: true },
             axisLabel: {
                 interval: yLabelInterval,
                 hideOverlap: yHideOverlap
             }
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
                data: seriesData, // Use the mapped data with original values
                label: {
                    show: true,
                    formatter: '{@[2]}' // Show the heat value (index 2 in the value array)
                 },
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
function buildCandlestickOptions(dataModel, normalizedRows) {
  if (!dataModel?.series?.length || !normalizedRows?.length) {
    console.warn('Candlestick: Missing dataModel, series, or normalizedRows');
    return {};
  }

  const keyField = dataModel.series[0]?.key; // Date field
  if (!keyField) {
    console.warn('Candlestick: Missing key field in first series config');
    return {};
  }

  // --- Preprocessing: Create a nested lookup map: date -> ticker -> row ---
  const dataLookup = new Map();
  let tickerField = 'ticker'; // Default assumption, try to find dynamically
  let foundTickerField = false;

  normalizedRows.forEach(row => {
    const dateCategory = getSafeValue(row, keyField);
    if (dateCategory === null || dateCategory === undefined) return; // Skip rows without a valid date

    // Attempt to find the ticker field more dynamically if not already found
    if (!foundTickerField) {
        const keys = Object.keys(row);
        const potentialTickerField = keys.find(k => k !== keyField && !['open', 'high', 'low', 'close'].includes(k.toLowerCase()));
        if (potentialTickerField) {
            tickerField = potentialTickerField;
            foundTickerField = true;
            // console.log("Determined ticker field:", tickerField);
        }
    }

    const tickerValue = getSafeValue(row, tickerField);
    if (tickerValue === null || tickerValue === undefined) return; // Skip rows without a valid ticker

    if (!dataLookup.has(dateCategory)) {
      dataLookup.set(dateCategory, new Map());
    }
    dataLookup.get(dateCategory).set(tickerValue, row);
  });
  // --- End Preprocessing ---

  // Extract unique categories (dates) and sort them
  const categories = [...dataLookup.keys()].sort();

  if (categories.length === 0) {
      console.warn('Candlestick: No categories found after processing.');
      return {};
  }
   if (!foundTickerField) {
       console.warn("Candlestick: Could not dynamically determine ticker field after processing. Assuming 'ticker'.");
   }

  const echartsSeries = dataModel.series.map(seriesConfig => {
    const seriesName = seriesConfig.name;
    const openField = seriesConfig.open;
    const closeField = seriesConfig.close;
    const lowField = seriesConfig.low;
    const highField = seriesConfig.high;

    if (!seriesName || !openField || !closeField || !lowField || !highField) {
      console.warn(`Candlestick: Skipping series due to missing config: ${JSON.stringify(seriesConfig)}`);
      return null;
    }

    // Map data using the lookup table
    const seriesData = categories.map(category => {
      const tickerMap = dataLookup.get(category);
      // Get the specific row for THIS seriesName and THIS category(date)
      const row = tickerMap?.get(seriesName);

      if (!row) {
        return [null, null, null, null]; // Null for missing points on the shared axis
      }

      const openVal = getSafeValue(row, openField);
      const closeVal = getSafeValue(row, closeField);
      const lowVal = getSafeValue(row, lowField);
      const highVal = getSafeValue(row, highField);

      // Optional: Re-add debug log if needed
       if (category === '2025-02-14') {
         console.log(`DEBUG [${seriesName} on ${category}]: Found Row: ${JSON.stringify(row)} -> [O:${openVal}, C:${closeVal}, L:${lowVal}, H:${highVal}]`);
       }

      // Echarts expects [open, close, lowest, highest]
      return [openVal, closeVal, lowVal, highVal];
    });

    return {
      name: seriesName,
      type: 'candlestick',
      data: seriesData,
       // Optional styling below
      itemStyle: {},
      emphasis: { itemStyle: { borderColor: '#555', borderWidth: 1 } },
      // markPoint: { data: [ { type: 'max', name: 'Max' }, { type: 'min', name: 'Min' } ] },
      // markLine: { data: [ { type: 'average', name: 'Avg' } ] }
    };
  }).filter(s => s !== null);

  if (echartsSeries.length === 0) {
      console.warn('Candlestick: No valid series could be generated.');
      return {};
  }

  // --- ECharts Options ---
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      // Consider using the default formatter first, it should work now.
      // If needed, uncomment and adapt the custom formatter from previous examples.
    },
    legend: {
      show: false,
      data: echartsSeries.map(s => s.name),
      bottom: 30,
      inactiveColor: '#777',
      textStyle: { color: '#333' }
    },
    xAxis: {
      type: 'category',
      data: categories, // *** Assign the definitive categories array ***
      axisLine: { lineStyle: { color: '#8392A5' } },
      splitLine: { show: false },
      axisLabel: { /* rotate: 30, interval: 'auto' */ } // Add label options if needed
    },
    yAxis: {
      type: 'value',
      scale: true,
      splitArea: { show: true },
      splitLine: { show: true, lineStyle: { color: ['#eee'] } },
      axisLabel: { color: '#333' }
    },
    grid: {
      left: '5%',
      right: '5%',
      bottom: '15%', // Increased slightly for dataZoom slider + potential labels
      containLabel: true
    },
     dataZoom: [
        { type: 'inside', start: 0, end: 100 },
        { type: 'slider', start: 0, end: 100, bottom: 10, height: 20 }
    ],
    series: echartsSeries
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
                 specificOptions = buildCandlestickOptions(props.data_model, normalizedRows);
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