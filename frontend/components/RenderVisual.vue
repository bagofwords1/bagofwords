<template>
    <div v-if="!isLoading && chartOptions && Object.keys(chartOptions).length > 0 && props.data?.rows?.length > 0" class="h-full">
      <VChart
        :key="chartRenderKey"
        class="chart"
        :option="chartOptions"
        autoresize
        :loading="isLoading"
       />
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
import type { EChartsOption, SeriesOption } from 'echarts'
import { graphic as EGraphic } from 'echarts'

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
    PieChart,
    BarChart,
    LineChart,
    ScatterChart,
    HeatmapChart,
    MapChart,
    CandlestickChart,
    TreemapChart,
    RadarChart,
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

// --- Basic Interfaces ---
interface DataRow {
    [key: string]: string | number | null | undefined;
}

interface SeriesConfig {
    name: string;
    key?: string;
    value?: string;
    x?: string;
    y?: string;
    open?: string;
    close?: string;
    low?: string;
    high?: string;
    dimensions?: string[];
    parentId?: string;
    id?: string;
}

interface DataModel {
    type: 'pie_chart' | 'bar_chart' | 'line_chart' | 'area_chart' | 'scatter_plot' | 'heatmap' | 'map' | 'candlestick' | 'treemap' | 'radar_chart' | string;
    series: SeriesConfig[];
    group_by?: string;  // NEW: Support groupBy for multi-series from grouped data
    map?: { mapName?: string; };
    radar?: { indicator?: { name: string, max?: number }[]; };
    columns?: any[];
}

interface ViewV2 {
    view?: {
        type?: string;
        x?: string;
        y?: string | string[];
        groupBy?: string;
        axisX?: { rotate?: number; interval?: number; show?: boolean; label?: string; };
        axisY?: { rotate?: number; interval?: number; show?: boolean; label?: string; };
        palette?: { theme?: string; colors?: string[]; };
        seriesStyles?: Array<{ key: string; label?: string; color?: string; }>;
        legend?: { show?: boolean; position?: string; };
        stacked?: boolean;
        smooth?: boolean;
        area?: boolean;
    };
    version?: string;
}

interface Widget {
    title?: string;
}

interface DataProp {
    rows?: DataRow[];
    columns?: any[];
}

const props = defineProps<{
    data: DataProp | null | undefined
    data_model: DataModel | null | undefined
    widget: Widget | null | undefined
    view?: ViewV2 | null
    resize?: boolean
}>()

const chartOptions = ref<EChartsOption>({})
const isLoading = ref(false)
const chartRenderKey = ref(0)

// --- Default color palette ---
const DEFAULT_PALETTE = ['#2563eb', '#16a34a', '#ea580c', '#dc2626', '#7c3aed', '#0891b2', '#db2777', '#84cc16']

// --- Helper: Get colors from view or default ---
function getColors(): string[] {
    const viewColors = props.view?.view?.palette?.colors
    if (Array.isArray(viewColors) && viewColors.length) return viewColors
    return DEFAULT_PALETTE
}

// --- Helper: Base Options ---
function getBaseOptions(): EChartsOption {
    return {
        grid: {
            containLabel: true,
            left: '3%',
            right: '4%',
            bottom: '10%',
            top: '12%'
        },
        legend: {
            show: props.view?.view?.legend?.show ?? false,
            orient: 'horizontal',
            right: 12,
            top: 12,
            type: 'scroll',
            itemWidth: 10,
            itemHeight: 6,
            icon: 'roundRect'
        },
        tooltip: {
            trigger: 'item',
            confine: true
        },
        series: []
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
    return val;
}

// --- Helper: Get axis label config ---
function getAxisLabelConfig(numCategories: number): { interval: number; rotate: number; hideOverlap: boolean } {
    // Check view settings first
    const viewRotate = props.view?.view?.axisX?.rotate
    const viewInterval = props.view?.view?.axisX?.interval
    
    if (viewRotate !== undefined || viewInterval !== undefined) {
        return {
            rotate: viewRotate ?? 45,
            interval: viewInterval ?? 0,
            hideOverlap: true
        }
    }
    
    // Default heuristics
    if (numCategories > 50) {
        return { interval: Math.max(1, Math.floor(numCategories / 20)), rotate: 45, hideOverlap: true }
    } else if (numCategories > 25) {
        return { interval: 1, rotate: 45, hideOverlap: true }
    } else if (numCategories > 10) {
        return { interval: 1, rotate: 45, hideOverlap: true }
    } else if (numCategories > 5) {
        return { interval: 0, rotate: 45, hideOverlap: true }
    }
    return { interval: 0, rotate: 0, hideOverlap: false }
}

function hexToRGBA(hex: string, alpha: number): string {
    if (typeof hex !== 'string' || !hex.startsWith('#')) return hex
    const raw = hex.replace('#', '')
    const normalized = raw.length === 3 ? raw.split('').map(c => c + c).join('') : raw
    if (normalized.length !== 6) return hex
    const num = parseInt(normalized, 16)
    if (Number.isNaN(num)) return hex
    const r = (num >> 16) & 255
    const g = (num >> 8) & 255
    const b = num & 255
    return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

// --- Builder: Pie Chart ---
function buildPieOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    const seriesConfig = dataModel.series[0];
    if (!seriesConfig || !seriesConfig.key || !seriesConfig.value) return {};

    const colors = getColors()
    const seriesData = normalizedRows.map((row, i) => ({
        name: getSafeValue(row, seriesConfig.key, 'string') ?? 'Unknown',
        value: getSafeValue(row, seriesConfig.value, 'number') ?? 0,
        itemStyle: { color: colors[i % colors.length] }
    })).filter(item => item.name !== 'Unknown' && item.value !== null);

    return {
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        series: [{
            name: seriesConfig.name,
            type: 'pie',
            radius: ['40%', '70%'],
            center: ['50%', '60%'],
            data: seriesData,
        }]
    };
}

// --- Builder: Cartesian Charts (Bar, Line, Area) with groupBy support ---
function buildCartesianOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    let chartType: 'bar' | 'line';
    let specificSeriesOptions: Partial<SeriesOption> = {};
    const barRadius = 4;
    const isHorizontal = props.view?.view?.horizontal === true || dataModel.horizontal === true;
    let barBorderRadius: number[] | undefined;
    
    const viewType = props.view?.view?.type
    const effectiveType = viewType || dataModel.type
    
    const isAreaChart = effectiveType === 'area_chart' || props.view?.view?.area === true;

    switch (effectiveType) {
        case 'line_chart':
            chartType = 'line';
            specificSeriesOptions = { smooth: props.view?.view?.smooth ?? true };
            break;
        case 'area_chart':
            chartType = 'line';
            specificSeriesOptions = { smooth: true };
            break;
        case 'bar_chart':
        default:
            chartType = 'bar';
            specificSeriesOptions = { barWidth: '60%' };
            break;
    }

    barBorderRadius = chartType === 'bar'
        ? (isHorizontal ? [0, barRadius, barRadius, 0] : [barRadius, barRadius, 0, 0])
        : undefined;

    // Determine x-axis key and groupBy from view or data_model
    const viewX = props.view?.view?.x
    const viewGroupBy = props.view?.view?.groupBy
    const dmGroupBy = dataModel.group_by
    
    const categoryKey = (viewX || dataModel.series[0]?.key)?.toLowerCase();
    const groupByKey = (viewGroupBy || dmGroupBy)?.toLowerCase();
    
    if (!categoryKey) return {};

    const categories = [...new Set(normalizedRows.map(row => 
        String(getSafeValue(row, categoryKey, 'string') ?? '')
    ))];

    const { interval: labelInterval, rotate: labelRotate, hideOverlap } = getAxisLabelConfig(categories.length)
    const colors = getColors()
    const showGrid = props.view?.view?.showGrid ?? (chartType === 'bar');

    let series: SeriesOption[] = [];

    const applyAreaAppearance = (entry: SeriesOption, color: string | undefined) => {
        if (!isAreaChart || chartType !== 'line') return
        const gradientFill = color
            ? new EGraphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: hexToRGBA(color, 0.35) },
                { offset: 1, color: hexToRGBA(color, 0.05) }
            ])
            : undefined
        entry.areaStyle = { color: gradientFill }
        entry.showSymbol = false
        entry.symbol = 'none'
        entry.lineStyle = { width: 2, color }
    }

    if (groupByKey) {
        // GroupBy mode: create one series per unique group value
        const groups = [...new Set(normalizedRows.map(row => 
            String(getSafeValue(row, groupByKey, 'string') ?? '')
        ))].filter(Boolean);

        const valueKey = (props.view?.view?.y 
            ? (Array.isArray(props.view.view.y) ? props.view.view.y[0] : props.view.view.y)
            : dataModel.series[0]?.value
        )?.toLowerCase();

        if (!valueKey) return {};

        series = groups.map((group, i) => {
            const seriesData = categories.map(cat => {
                const row = normalizedRows.find(r => 
                    String(getSafeValue(r, categoryKey, 'string') ?? '') === cat &&
                    String(getSafeValue(r, groupByKey, 'string') ?? '') === group
                );
                return row ? getSafeValue(row, valueKey, 'number') : null;
            });

            // Find series style override
            const styleOverride = props.view?.view?.seriesStyles?.find(s => s.key === group)

            const seriesEntry: SeriesOption = {
                name: styleOverride?.label || group,
                type: chartType,
                data: seriesData,
                itemStyle: barBorderRadius
                    ? { color: styleOverride?.color || colors[i % colors.length], borderRadius: barBorderRadius }
                    : { color: styleOverride?.color || colors[i % colors.length] },
                ...specificSeriesOptions
            }
            applyAreaAppearance(seriesEntry, styleOverride?.color || colors[i % colors.length])
            return seriesEntry;
        });
    } else {
        // Traditional mode: each series config is a series
        series = dataModel.series.map((seriesConfig, i) => {
            const valueKey = seriesConfig.value?.toLowerCase();
            if (!valueKey) return null;

            const seriesDataMap = new Map<string, number | null>();
            normalizedRows.forEach(row => {
                const cat = String(getSafeValue(row, categoryKey, 'string') ?? '');
                const val = getSafeValue(row, valueKey, 'number');
                if (cat) {
                    seriesDataMap.set(cat, val);
                }
            });
            const seriesData = categories.map(cat => seriesDataMap.get(cat) ?? null);

            const styleOverride = props.view?.view?.seriesStyles?.find(s => s.key === seriesConfig.name)

            const seriesEntry: SeriesOption = {
                name: styleOverride?.label || seriesConfig.name,
                type: chartType,
                data: seriesData,
                itemStyle: barBorderRadius
                    ? { color: styleOverride?.color || colors[i % colors.length], borderRadius: barBorderRadius }
                    : { color: styleOverride?.color || colors[i % colors.length] },
                ...specificSeriesOptions
            }
            applyAreaAppearance(seriesEntry, styleOverride?.color || colors[i % colors.length])
            return seriesEntry;
        }).filter(Boolean) as SeriesOption[];
    }

    const legendShouldShow = series.length > 1 ? (props.view?.view?.legend?.show ?? true) : false;
    const categoryAxisNameRaw = props.view?.view?.axisX?.label;
    const valueAxisNameRaw = props.view?.view?.axisY?.label;
    const categoryAxisName = categoryAxisNameRaw && categoryAxisNameRaw.trim().length ? categoryAxisNameRaw : undefined;
    const valueAxisName = valueAxisNameRaw && valueAxisNameRaw.trim().length ? valueAxisNameRaw : undefined;

    const categoryAxis = {
        type: 'category',
        boundaryGap: chartType === 'bar',
        data: categories,
        name: categoryAxisName,
        axisLabel: isHorizontal
            ? { interval: 0, rotate: 0, hideOverlap: false }
            : { interval: labelInterval, rotate: labelRotate, hideOverlap },
        splitLine: { show: false }
    }

    const gridColor = '#f7f7f9';

    const valueAxis = {
        type: 'value',
        name: valueAxisName,
        splitLine: showGrid ? { show: true, lineStyle: { color: gridColor, width: 1 } } : { show: false }
    }

    return {
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross', label: { backgroundColor: '#6a7985' } }
        },
        legend: {
            show: legendShouldShow,
            data: series.map(s => (s as any).name),
            right: 12,
            top: 12,
            itemWidth: 10,
            itemHeight: 6,
            icon: 'roundRect'
        },
        xAxis: isHorizontal ? valueAxis : categoryAxis,
        yAxis: isHorizontal ? categoryAxis : valueAxis,
        dataZoom: [
            { type: 'inside', xAxisIndex: 0, filterMode: 'weakFilter' },
            { show: false, type: 'slider', xAxisIndex: 0, start: 0, end: 100, bottom: '1%', height: 20 }
        ],
        series: series
    };
}

// --- Builder: Scatter Plot ---
function buildScatterOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    const xKey = dataModel.series[0]?.x?.toLowerCase() || dataModel.series[0]?.key?.toLowerCase();
    const yKey = dataModel.series[0]?.y?.toLowerCase() || dataModel.series[0]?.value?.toLowerCase();

    if (!xKey || !yKey) return {};

    const colors = getColors()
    const seriesData = normalizedRows.map(row => {
        const xVal = getSafeValue(row, xKey, 'number');
        const yVal = getSafeValue(row, yKey, 'number');
        if (xVal === null || yVal === null) return null;
        return [xVal, yVal];
    }).filter(Boolean);

    return {
        tooltip: { trigger: 'item' },
        xAxis: { type: 'value', name: dataModel.series[0]?.x || 'X Axis', splitLine: { lineStyle: { type: 'dashed' } } },
        yAxis: { type: 'value', name: dataModel.series[0]?.y || 'Y Axis', splitLine: { lineStyle: { type: 'dashed' } } },
        series: [{
            name: dataModel.series[0]?.name || 'Scatter Data',
            type: 'scatter',
            symbolSize: 10,
            data: seriesData,
            itemStyle: { color: colors[0] }
        }]
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

    const { interval: xLabelInterval, rotate: xLabelRotate, hideOverlap: xHideOverlap } = getAxisLabelConfig(xCategories.length)

    const seriesData = normalizedRows.map(row => {
        const xVal = getSafeValue(row, xKey, 'string');
        const yVal = getSafeValue(row, yKey, 'string');
        const heatVal = getSafeValue(row, valueKey, 'number');
        const xIndex = xCategories.indexOf(xVal ?? '');
        const yIndex = yCategories.indexOf(yVal ?? '');
        if (xIndex === -1 || yIndex === -1 || heatVal === null) return null;
        return { value: [xIndex, yIndex, heatVal], originalX: xVal, originalY: yVal };
    }).filter(item => item !== null);

    const maxHeat = Math.max(...seriesData.map(d => d?.value[2] ?? 0) as number[], 0);

    return {
        tooltip: { position: 'top', formatter: (params: any) => {
            const d = params.data;
            if (d?.value?.length === 3) {
                return `<b>${config.value || 'Value'}</b>: ${d.value[2]}<br/><b>${config.x || 'X'}</b>: ${d.originalX}<br/><b>${config.y || 'Y'}</b>: ${d.originalY}`;
            }
            return '';
        }},
        grid: { height: '60%', top: '10%', bottom: '25%', left: '10%', containLabel: true },
        xAxis: { type: 'category', data: xCategories, splitArea: { show: true }, axisLabel: { interval: xLabelInterval, rotate: xLabelRotate, hideOverlap: xHideOverlap } },
        yAxis: { type: 'category', data: yCategories, splitArea: { show: true } },
        visualMap: { min: 0, max: maxHeat, calculable: true, orient: 'horizontal', left: 'center', bottom: '5%' },
        series: [{ name: config?.name || 'Heatmap', type: 'heatmap', data: seriesData, label: { show: true, formatter: '{@[2]}' } }]
    };
}

// --- Builder: Map ---
function buildMapOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    const mapName = dataModel.map?.mapName || 'world';
    const config = dataModel.series[0];
    const regionKey = config?.key?.toLowerCase();
    const valueKey = config?.value?.toLowerCase();
    if (!regionKey || !valueKey) return {};

    const seriesData = normalizedRows.map(row => ({
        name: getSafeValue(row, regionKey, 'string') ?? 'Unknown',
        value: getSafeValue(row, valueKey, 'number') ?? 0
    })).filter(item => item.name !== 'Unknown');

    const maxVal = Math.max(...seriesData.map(d => d.value), 0);

    return {
        tooltip: { trigger: 'item', formatter: '{b}: {c}' },
        visualMap: { left: 'right', min: 0, max: maxVal, inRange: { color: ['#e0f3f8', '#abd9e9', '#74add1', '#4575b4', '#313695'] }, calculable: true },
        series: [{ name: config?.name || mapName, type: 'map', map: mapName, roam: true, emphasis: { label: { show: true } }, data: seriesData }]
    };
}

// --- Builder: Candlestick ---
function buildCandlestickOptions(dataModel: DataModel, normalizedRows: DataRow[]): EChartsOption {
    if (!dataModel?.series?.length || !normalizedRows?.length) return {};

    const keyField = dataModel.series[0]?.key;
    if (!keyField) return {};

    const colors = getColors()
    let dataLookup = new Map<any, any>();
    let tickerField = '';
    let foundTickerField = false;

    if (normalizedRows.length > 0) {
        const firstRowKeys = Object.keys(normalizedRows[0]);
        const potentialTickerField = firstRowKeys.find(k =>
            k !== keyField.toLowerCase() && !['open', 'high', 'low', 'close'].includes(k.toLowerCase())
        );
        if (potentialTickerField) {
            tickerField = potentialTickerField;
            foundTickerField = true;
        }
    }

    normalizedRows.forEach(row => {
        const dateCategory = getSafeValue(row, keyField);
        if (dateCategory === null || dateCategory === undefined) return;

        if (foundTickerField) {
            const tickerValue = getSafeValue(row, tickerField);
            if (tickerValue === null || tickerValue === undefined) return;
            if (!dataLookup.has(dateCategory)) dataLookup.set(dateCategory, new Map());
            dataLookup.get(dateCategory).set(tickerValue, row);
        } else {
            dataLookup.set(dateCategory, row);
        }
    });

    const categories = [...dataLookup.keys()].map(String).sort((a, b) => new Date(a).getTime() - new Date(b).getTime());
    if (categories.length === 0) return {};

    const echartsSeries = dataModel.series.map((seriesConfig, i) => {
        const { name: seriesName, open: openField, close: closeField, low: lowField, high: highField } = seriesConfig;
        if (!seriesName || !openField || !closeField || !lowField || !highField) return null;

        const seriesData = categories.map(category => {
            let row = foundTickerField ? dataLookup.get(category)?.get(seriesName) : dataLookup.get(category);
            if (foundTickerField && dataModel.series.indexOf(seriesConfig) > 0 && !foundTickerField) return null;
            if (!row) return [null, null, null, null];
            return [getSafeValue(row, openField), getSafeValue(row, closeField), getSafeValue(row, lowField), getSafeValue(row, highField)];
        }).filter(item => item !== null);

        if (seriesData.length === 0) return null;

        return { name: seriesName, type: 'candlestick', data: seriesData, itemStyle: { color: colors[i % colors.length] } };
    }).filter(Boolean);

    if (echartsSeries.length === 0) return {};

    return {
        tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
        legend: { show: echartsSeries.length > 1, data: echartsSeries.map(s => (s as any).name), bottom: 30 },
        xAxis: { type: 'category', data: categories, axisLine: { lineStyle: { color: '#8392A5' } } },
        yAxis: { type: 'value', scale: true, splitArea: { show: true } },
        grid: { left: '5%', right: '5%', bottom: '15%', containLabel: true },
        dataZoom: [{ type: 'inside', start: 0, end: 100 }, { type: 'slider', start: 0, end: 100, bottom: 10, height: 20 }],
        series: echartsSeries
    };
}

// --- Builder: Treemap ---
function buildTreemapOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    const config = dataModel.series[0];
    const idKey = config?.id?.toLowerCase() || 'id';
    const parentIdKey = config?.parentId?.toLowerCase() || 'parentid';
    const valueKey = config?.value?.toLowerCase();
    const nameKey = config?.key?.toLowerCase() || config?.name?.toLowerCase() || 'name';

    if (!valueKey || !nameKey) return {};

    const map = new Map<string | number, any>();
    const tree: any[] = [];

    normalizedRows.forEach(row => {
        const id = getSafeValue(row, idKey, 'any');
        const value = getSafeValue(row, valueKey, 'number');
        const name = getSafeValue(row, nameKey, 'string');
        if (value === null || name === null || id === null) return;
        map.set(id, { id, name, value, children: [] });
    });

    map.forEach(node => {
        const parentId = getSafeValue(normalizedRows.find(r => getSafeValue(r, idKey, 'any') === node.id)!, parentIdKey, 'any');
        if (parentId !== null && map.has(parentId)) {
            map.get(parentId).children.push(node);
        } else {
            tree.push(node);
        }
    });

    return {
        tooltip: { formatter: '{b}: {c}' },
        series: [{ name: config?.name || 'Treemap', type: 'treemap', visibleMin: 300, label: { show: true }, data: tree }]
    };
}

// --- Builder: Radar Chart ---
function buildRadarOptions(normalizedRows: DataRow[], dataModel: DataModel): EChartsOption {
    let indicators: { name: string, max?: number }[] = [];
    
    if (dataModel.radar?.indicator) {
        indicators = dataModel.radar.indicator;
    } else if (dataModel.series[0]?.dimensions?.length) {
        const dimKeys = dataModel.series[0].dimensions.map(d => d.toLowerCase());
        const maxValues: { [key: string]: number } = {};
        dimKeys.forEach(key => maxValues[key] = 0);

        normalizedRows.forEach(row => {
            dimKeys.forEach(key => {
                const val = getSafeValue(row, key, 'number');
                if (val !== null && val > maxValues[key]) maxValues[key] = val as number;
            });
        });
        indicators = dimKeys.map(key => ({ name: dataModel.series[0].dimensions?.find(d => d.toLowerCase() === key) || key, max: maxValues[key] * 1.1 }));
    } else {
        return {};
    }

    const colors = getColors()
    const seriesDataMap = new Map<string, number[]>();
    const seriesNames: string[] = [];

    dataModel.series.forEach((seriesConfig, i) => {
        const seriesName = seriesConfig.name;
        const dimKeys = seriesConfig.dimensions?.map(d => d.toLowerCase()) || indicators.map(ind => ind.name.toLowerCase());
        const seriesRow = normalizedRows.find(row => getSafeValue(row, seriesConfig.key || 'name', 'string') === seriesName);

        if (seriesRow) {
            const seriesValues = dimKeys.map(key => (getSafeValue(seriesRow, key, 'number') ?? 0) as number);
            seriesDataMap.set(seriesName, seriesValues);
            seriesNames.push(seriesName);
        }
    });

    const series = [...seriesDataMap.entries()].map(([name, values], i) => ({
        name,
        value: values,
        itemStyle: { color: colors[i % colors.length] }
    }));

    return {
        tooltip: { trigger: 'item' },
        legend: { data: seriesNames, bottom: '1%' },
        radar: { indicator: indicators, shape: 'circle', center: ['50%', '55%'], radius: '65%' },
        series: [{ type: 'radar', data: series }]
    };
}

// --- Main Dispatcher Function ---
async function buildChartOptions() {
    isLoading.value = true;
    chartOptions.value = {};

    if (!props.data_model || !props.data?.rows?.length) {
        isLoading.value = false;
        chartRenderKey.value++;
        return;
    }

    const chartType = props.view?.view?.type || props.data_model.type;
    const normalizedRows = normalizeRows(props.data.rows);

    let specificOptions: EChartsOption = {};
    const baseOptions = getBaseOptions();

    try {
        switch (chartType) {
            case 'pie_chart':
                specificOptions = buildPieOptions(normalizedRows, props.data_model);
                break;
            case 'bar_chart':
            case 'line_chart':
            case 'area_chart':
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
                specificOptions = {};
                break;
        }

        chartOptions.value = { ...baseOptions, ...specificOptions };
    } catch (error) {
        console.error(`Error building options for ${chartType}:`, error);
        chartOptions.value = {};
    } finally {
        await new Promise(resolve => setTimeout(resolve, 50));
        isLoading.value = false;
        chartRenderKey.value++;
    }
}

watch([() => props.data?.rows, () => props.data_model, () => props.view], () => {
    buildChartOptions();
}, { immediate: true, deep: true });

</script>

<style scoped>
.chart {
    width: 100%;
    min-height: 100px;
    height: 100%;
}
</style>
