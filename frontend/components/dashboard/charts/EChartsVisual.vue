<template>
  <div class="h-full w-full flex flex-col" :style="wrapperStyle">
    <div v-if="!isLoading && chartOptions && Object.keys(chartOptions).length > 0 && (data?.rows?.length || 0) > 0" class="flex-1 min-h-0">
      <VChart :key="chartKey" class="chart" :option="chartOptions" autoresize :loading="isLoading" />
    </div>
    <div v-else-if="isLoading" class="flex-1 flex items-center justify-center text-gray-500">Loading Chart...</div>
    <div v-else-if="!(data?.rows?.length > 0)" class="flex-1 flex items-center justify-center text-gray-400">No data to display.</div>
    <div v-else class="flex-1 flex items-center justify-center text-gray-400">Chart configuration error or unsupported type.</div>
  </div>
</template>

<script setup lang="ts">
import { toRefs, ref, watch, computed } from 'vue'
import { useDashboardTheme } from '@/components/dashboard/composables/useDashboardTheme'
import { use } from 'echarts/core'
import { graphic as EGraphic } from 'echarts'
import { CanvasRenderer } from 'echarts/renderers'
import { PieChart, BarChart, LineChart, ScatterChart, HeatmapChart, CandlestickChart, TreemapChart, RadarChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, GridComponent, LegendComponent, VisualMapComponent, DataZoomComponent, MarkLineComponent, MarkPointComponent, AriaComponent } from 'echarts/components'

use([
  CanvasRenderer,
  PieChart, BarChart, LineChart, ScatterChart, HeatmapChart, CandlestickChart, TreemapChart, RadarChart,
  TitleComponent, TooltipComponent, GridComponent, LegendComponent, VisualMapComponent, DataZoomComponent, MarkLineComponent, MarkPointComponent, AriaComponent,
])

const props = defineProps<{
  widget?: any
  step?: any
  data?: any
  data_model?: any
  view?: Record<string, any> | null
  reportThemeName?: string | null
  reportOverrides?: Record<string, any> | null
}>()

const { reportThemeName, reportOverrides } = toRefs(props)
const { tokens } = useDashboardTheme(reportThemeName, reportOverrides, props.view || null)

type EChartsOption = Record<string, any>

const isLoading = ref(false)
const chartOptions = ref<EChartsOption>({})
const chartKey = ref(0)

// Card-level wrapper style - default transparent with no border
const wrapperStyle = computed(() => {
  const style = (props.view?.style as any) || {}
  const bg = style.cardBackground
  const border = style.cardBorder
  const out: Record<string, any> = {
    backgroundColor: 'transparent',
    border: 'none'
  }
  // Only override if explicitly set
  if (bg) out.backgroundColor = bg
  if (border && border !== 'none' && typeof border === 'string' && border.trim().length) {
    out.border = `1px solid ${border}`
  }
  return out
})

// --- Helpers ---
function normalizeType(t?: string | null): string {
  const v = String(t || '').toLowerCase()
  if (v === 'pie') return 'pie_chart'
  if (v === 'bar') return 'bar_chart'
  if (v === 'line') return 'line_chart'
  if (v === 'area') return 'area_chart'
  return v
}

function normalizeRows(rows: any[] | undefined): any[] {
  if (!Array.isArray(rows)) return []
  return rows.map(r => {
    const o: any = {}
    Object.keys(r).forEach(k => (o[k.toLowerCase()] = r[k]))
    return o
  })
}

function getSafeValue(row: any, key: string | undefined, type: 'string' | 'number' | 'any' = 'any'): any {
  if (!key) return null
  const val = row[key.toLowerCase()]
  if (val === null || val === undefined) return null
  if (type === 'number') {
    const num = parseFloat(String(val))
    return isNaN(num) ? null : num
  }
  if (type === 'string') return String(val)
  return val
}

// Get colors from view, theme, or fallback
function getColors(): any[] {
  // Check view.view (new v2 schema)
  const viewV2Colors = props.view?.view?.palette?.colors
  if (Array.isArray(viewV2Colors) && viewV2Colors.length) return viewV2Colors
  
  // Check legacy view.options.colors
  const viewColors = (props.view?.options as any)?.colors
  if (Array.isArray(viewColors) && viewColors.length) return viewColors
  
  // Theme palette
  const themePalette = tokens.value?.palette as any
  if (Array.isArray(themePalette) && themePalette.length) return themePalette
  
  // Default fallback
  return ['#2563eb', '#16a34a', '#ea580c', '#dc2626', '#7c3aed', '#0891b2', '#db2777', '#84cc16']
}

function resolveColorInput(input: any): any {
  if (!input) return undefined
  if (typeof input === 'string') return input
  if (typeof input === 'object' && Array.isArray(input.colorStops)) {
    const x = Number(input.x ?? 0)
    const y = Number(input.y ?? 0)
    const x2 = Number(input.x2 ?? 1)
    const y2 = Number(input.y2 ?? 0)
    return new EGraphic.LinearGradient(x, y, x2, y2, input.colorStops)
  }
  return input
}

function getAxisLabelConfig(numCategories: number): { interval: number; rotate: number; hideOverlap: boolean } {
  // Check view.view (new v2 schema)
  const viewV2 = props.view?.view
  if (viewV2?.axisX) {
    return {
      rotate: viewV2.axisX.rotate ?? 45,
      interval: viewV2.axisX.interval ?? 0,
      hideOverlap: true
    }
  }
  
  // Check legacy view settings
  const viewInterval = props.view?.xAxisLabelInterval
  const themeInterval = tokens.value?.axis?.xLabelInterval
  const viewRotate = props.view?.xAxisLabelRotate
  const themeRotate = tokens.value?.axis?.xLabelRotate
  
  if (viewRotate !== undefined || viewInterval !== undefined) {
    return {
      rotate: viewRotate ?? themeRotate ?? 45,
      interval: viewInterval ?? themeInterval ?? 0,
      hideOverlap: true
    }
  }
  
  // Default heuristics based on category count
  if (numCategories > 50) return { interval: Math.max(1, Math.floor(numCategories / 20)), rotate: 45, hideOverlap: true }
  if (numCategories > 25) return { interval: 1, rotate: 45, hideOverlap: true }
  if (numCategories > 10) return { interval: 1, rotate: 45, hideOverlap: true }
  if (numCategories > 5) return { interval: 0, rotate: 45, hideOverlap: true }
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

function getBaseOptions(): EChartsOption {
  const xVisible = props.view?.xAxisVisible ?? props.view?.view?.axisX?.show ?? true
  const yVisible = props.view?.yAxisVisible ?? props.view?.view?.axisY?.show ?? true
  const legendVisible = props.view?.legendVisible ?? props.view?.view?.legend?.show ?? false
  const containLabel = xVisible || yVisible
  const topPad: number | string = legendVisible ? 36 : 18
  const bottomPad: number | string = legendVisible ? 24 : 12
  const leftPad: number | string = 24
  const rightPad: number | string = 24
  
  return {
    color: undefined,
    backgroundColor: (props.view?.style as any)?.backgroundColor || (props.view?.options as any)?.backgroundColor || 'transparent',
    title: { show: false },
    grid: { containLabel, left: leftPad, right: rightPad, bottom: bottomPad, top: topPad },
    legend: {
      show: false,
      right: 12,
      top: 12,
      orient: 'horizontal',
      itemWidth: 10,
      itemHeight: 6,
      icon: 'roundRect',
      textStyle: { color: tokens.value?.legend?.textColor, fontSize: 11 }
    },
    tooltip: { trigger: 'item', confine: true, ...(tokens.value?.tooltip || {}) },
    series: []
  }
}

// --- Chart Builders ---
function buildPieOptions(rows: any[], dm: any): EChartsOption {
  const cfg = dm?.series?.[0] || {}
  if (!cfg?.key || !cfg?.value) return {}
  
  const colors = getColors()
  const data = rows
    .map((r: any, i: number) => ({
      name: r[cfg.key.toLowerCase()],
      value: Number(r[cfg.value.toLowerCase()]),
      itemStyle: { color: resolveColorInput(colors[i % colors.length]) }
    }))
    .filter((d: any) => d.name != null && !Number.isNaN(d.value))
  
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    series: [{ name: cfg.name, type: 'pie', radius: ['40%', '70%'], center: ['50%', '60%'], data }]
  }
}

function buildCartesianOptions(rows: any[], dm: any): EChartsOption {
  const t = normalizeType((props.view?.view?.type || props.view as any)?.type || dm?.type)
  const variant = props.view?.variant || props.view?.view?.area || (t === 'area_chart' ? 'area' : undefined)
  const chartType = t === 'line_chart' || variant === 'area' || t === 'area_chart' ? 'line' : 'bar'
  const isHorizontal = props.view?.view?.horizontal === true || dm?.horizontal === true
  const barRadiusValue = 4
  const barBorderRadius =
    chartType === 'bar'
      ? isHorizontal
        ? [0, barRadiusValue, barRadiusValue, 0]
        : [barRadiusValue, barRadiusValue, 0, 0]
      : undefined
  
  // Determine x-axis key and groupBy
  const viewV2 = props.view?.view
  const categoryKey = (viewV2?.x || dm?.series?.[0]?.key)?.toLowerCase()
  const groupByKey = (viewV2?.groupBy || dm?.group_by)?.toLowerCase()
  
  if (!categoryKey) return {}
  
  const categories = Array.from(new Set(rows.map((r: any) => String(r[categoryKey] ?? ''))))
  const { interval, rotate, hideOverlap } = getAxisLabelConfig(categories.length)
  const colors = getColors()
  const axisColors = { ...(tokens.value?.axis || {}), ...((props.view?.style as any)?.axis || {}) }
  const xVisible = props.view?.xAxisVisible ?? viewV2?.axisX?.show ?? true
  const yVisible = props.view?.yAxisVisible ?? viewV2?.axisY?.show ?? true
  const gridSetting = viewV2?.showGrid
  const legacyGrid = (props.view as any)?.showGridLines
  const showGrid =
    typeof gridSetting === 'boolean'
      ? gridSetting
      : legacyGrid === true
        ? true
        : chartType === 'bar'

  let series: any[] = []

  if (groupByKey) {
    // GroupBy mode: create one series per unique group value
    const groups = Array.from(new Set(rows.map((r: any) => String(r[groupByKey] ?? '')))).filter(Boolean)
    
    const valueKey = (viewV2?.y 
      ? (Array.isArray(viewV2.y) ? viewV2.y[0] : viewV2.y)
      : dm?.series?.[0]?.value
    )?.toLowerCase()

    if (!valueKey) return {}

    series = groups.map((group: string, i: number) => {
      const data = categories.map(cat => {
        const row = rows.find((r: any) => 
          String(r[categoryKey] ?? '') === cat && String(r[groupByKey] ?? '') === group
        )
        const v = row ? Number(row[valueKey]) : null
        return Number.isNaN(v as number) ? null : v
      })

      // Find series style override from v2 schema
      const styleOverride = viewV2?.seriesStyles?.find((s: any) => s.key === group)
      const color = resolveColorInput(styleOverride?.color || colors[i % colors.length])

      const base: any = {
        name: styleOverride?.label || group,
        type: chartType,
        data,
        itemStyle: barBorderRadius ? { color, borderRadius: barBorderRadius } : { color }
      }
      if (chartType === 'line' && (variant === 'area' || t === 'area_chart')) {
        const solidColor = typeof color === 'string' ? color : undefined
        const gradientFill = solidColor
          ? new EGraphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: hexToRGBA(solidColor, 0.35) },
              { offset: 1, color: hexToRGBA(solidColor, 0.05) },
            ])
          : color
        base.areaStyle = { color: gradientFill }
        base.showSymbol = false
        base.symbol = 'none'
        base.lineStyle = { width: 2, color: solidColor }
      }
      if (chartType === 'line' && (viewV2?.smooth || props.view?.variant === 'smooth')) base.smooth = true
      return base
    })
  } else {
    // Traditional mode: each series config is a series
    series = (dm?.series || [])
      .map((s: any, i: number) => {
        const valueKey = s?.value?.toLowerCase()
        if (!valueKey) return null
        
        const data = categories.map(cat => {
          const row = rows.find((r: any) => String(r[categoryKey] ?? '') === cat)
          const v = row ? Number(row[valueKey]) : null
          return Number.isNaN(v as number) ? null : v
        })
        
        const styleOverride = viewV2?.seriesStyles?.find((st: any) => st.key === s.name)
        const color = resolveColorInput(styleOverride?.color || colors[i % colors.length])
        
        const base: any = {
          name: styleOverride?.label || s.name,
          type: chartType,
          data,
          itemStyle: barBorderRadius ? { color, borderRadius: barBorderRadius } : { color }
        }
        if (chartType === 'line' && (variant === 'area' || t === 'area_chart')) {
          const solidColor = typeof color === 'string' ? color : undefined
          const gradientFill = solidColor
            ? new EGraphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: hexToRGBA(solidColor, 0.35) },
                { offset: 1, color: hexToRGBA(solidColor, 0.05) },
              ])
            : color
          base.areaStyle = { color: gradientFill }
          base.showSymbol = false
          base.symbol = 'none'
          base.lineStyle = { width: 2, color: solidColor }
        }
        if (chartType === 'line' && (viewV2?.smooth || props.view?.variant === 'smooth')) base.smooth = true
        return base
      })
      .filter(Boolean)
  }

  const axisXLabel = (viewV2?.axisX?.label || '').trim()
  const axisYLabel = (viewV2?.axisY?.label || '').trim()

  const categoryAxis = {
    type: 'category',
    data: categories,
    name: axisXLabel || undefined,
    show: xVisible,
    axisLabel: isHorizontal
      ? { interval: 0, rotate: 0, hideOverlap: false, color: axisColors.yLabelColor }
      : { interval, rotate, hideOverlap, color: axisColors.xLabelColor },
    axisLine: { lineStyle: { color: isHorizontal ? axisColors.yLineColor : axisColors.xLineColor } },
    splitLine: { show: false }
  }

  const gridColor = '#f4f5f7'

  const valueAxis = {
    type: 'value',
    name: axisYLabel || undefined,
    show: yVisible,
    axisLabel: { color: axisColors.yLabelColor },
    axisLine: { lineStyle: { color: axisColors.yLineColor } },
    splitLine: showGrid ? { show: true, lineStyle: { color: gridColor, width: 1 } } : { show: false }
  }

  // Respect user's legend setting, default to hidden
  const legendShouldShow = props.view?.legendVisible ?? viewV2?.legend?.show ?? false

  // Only include legend config when it should be shown
  const legendConfig = legendShouldShow ? {
    show: true,
    data: series.map(s => s.name),
    right: 12,
    top: 12,
    orient: 'horizontal',
    itemWidth: 10,
    itemHeight: 6,
    icon: 'roundRect',
    textStyle: { color: tokens.value?.legend?.textColor, fontSize: 11 }
  } : { show: false }

  return {
    tooltip: { trigger: 'axis' },
    xAxis: isHorizontal ? valueAxis : categoryAxis,
    yAxis: isHorizontal ? categoryAxis : valueAxis,
    legend: legendConfig,
    series
  }
}

function buildScatterOptions(rows: any[], dm: any): EChartsOption {
  const s = dm?.series?.[0] || {}
  const viewV2 = props.view?.view
  const xKey = (viewV2?.x || s?.x || s?.key || '').toLowerCase()
  const yKey = (viewV2?.y || s?.y || s?.value || '').toLowerCase()
  if (!xKey || !yKey) return {}
  
  const colors = getColors()
  const data = rows
    .map((r: any) => [Number(r[xKey]), Number(r[yKey])])
    .filter((d: any[]) => !d.some(v => Number.isNaN(v)))

  const axisColors = props.view?.style?.axis || tokens.value?.axis || {}
  const xVisible = props.view?.xAxisVisible ?? viewV2?.axisX?.show ?? true
  const yVisible = props.view?.yAxisVisible ?? viewV2?.axisY?.show ?? true

  return {
    tooltip: { trigger: 'item' },
    xAxis: { type: 'value', name: s?.x || s?.key || 'X', show: xVisible, axisLabel: { color: axisColors.xLabelColor }, axisLine: { lineStyle: { color: axisColors.xLineColor } } },
    yAxis: { type: 'value', name: s?.y || s?.value || 'Y', show: yVisible, axisLabel: { color: axisColors.yLabelColor }, axisLine: { lineStyle: { color: axisColors.yLineColor } } },
    series: [{ type: 'scatter', name: s?.name || 'Scatter', data, itemStyle: { color: resolveColorInput(colors[0]) } }]
  }
}

function buildHeatmapOptions(rows: any[], dm: any): EChartsOption {
  const cfg = dm?.series?.[0] || {}
  const xKey = (cfg?.x || cfg?.key || '').toLowerCase()
  const yKey = (cfg?.y || '').toLowerCase()
  const vKey = (cfg?.value || '').toLowerCase()
  if (!xKey || !yKey || !vKey) return {}
  
  const xCats = Array.from(new Set(rows.map((r: any) => String(r[xKey] ?? '')).filter(Boolean)))
  const yCats = Array.from(new Set(rows.map((r: any) => String(r[yKey] ?? '')).filter(Boolean)))
  const { interval, rotate, hideOverlap } = getAxisLabelConfig(xCats.length)
  
  const seriesData = rows
    .map((r: any) => {
      const xi = xCats.indexOf(String(r[xKey] ?? ''))
      const yi = yCats.indexOf(String(r[yKey] ?? ''))
      const val = Number(r[vKey])
      if (xi === -1 || yi === -1 || Number.isNaN(val)) return null
      return [xi, yi, val]
    })
    .filter(Boolean)
  
  const maxVal = seriesData.reduce((m: number, d: any) => Math.max(m, d![2]), 0)
  const colors = getColors()
  const heatColors = colors.slice(0, 4).map((c: any, idx: number) => {
    if (typeof c === 'string') return c
    if (c?.colorStops?.[0]?.color) return c.colorStops[0].color
    return ['#22d3ee','#38bdf8','#a78bfa','#fbbf24'][idx] || '#22d3ee'
  })
  
  return {
    tooltip: { position: 'top' },
    xAxis: { type: 'category', data: xCats, axisLabel: { interval, rotate, hideOverlap } },
    yAxis: { type: 'category', data: yCats },
    visualMap: { min: 0, max: maxVal, orient: 'horizontal', left: 'center', bottom: '5%', inRange: { color: heatColors } },
    series: [{ type: 'heatmap', data: seriesData, label: { show: true, formatter: '{@[2]}' } }]
  }
}

function buildCandlestickOptions(rows: any[], dm: any): EChartsOption {
  const s = dm?.series?.[0] || {}
  const keyField = (s?.key || '').toLowerCase()
  const openF = (s?.open || 'open').toLowerCase()
  const closeF = (s?.close || 'close').toLowerCase()
  const lowF = (s?.low || 'low').toLowerCase()
  const highF = (s?.high || 'high').toLowerCase()
  if (!keyField) return {}
  
  const sorted = [...rows].sort((a: any, b: any) => new Date(String(a[keyField] || '')).getTime() - new Date(String(b[keyField] || '')).getTime())
  const categories = sorted.map((r: any) => String(r[keyField] || ''))
  const data = sorted.map((r: any) => [Number(r[openF]), Number(r[closeF]), Number(r[lowF]), Number(r[highF])])
  
  return {
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: categories },
    yAxis: { type: 'value', scale: true },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 10, height: 20 }],
    series: [{ type: 'candlestick', name: s?.name || 'OHLC', data }]
  }
}

function buildTreemapOptions(rows: any[], dm: any): EChartsOption {
  const cfg = dm?.series?.[0] || {}
  const idKey = (cfg?.id || 'id').toLowerCase()
  const parentKey = (cfg?.parentId || 'parentid').toLowerCase()
  const nameKey = (cfg?.key || cfg?.name || 'name').toLowerCase()
  const valueKey = (cfg?.value || '').toLowerCase()
  if (!valueKey || !nameKey) return {}
  
  const nodes = rows.map((r: any) => ({ id: r[idKey], parentId: r[parentKey], name: r[nameKey], value: Number(r[valueKey]) }))
  const idMap = new Map<any, any>()
  nodes.forEach(n => idMap.set(n.id, { id: n.id, name: n.name, value: n.value, children: [] as any[] }))
  const tree: any[] = []
  nodes.forEach(n => {
    const node = idMap.get(n.id)
    const parent = idMap.get(n.parentId)
    if (parent) parent.children.push(node)
    else tree.push(node)
  })
  
  return { series: [{ type: 'treemap', name: cfg?.name || 'Treemap', data: tree, label: { show: true } }] }
}

function buildRadarOptions(rows: any[], dm: any): EChartsOption {
  const cfg = dm?.series || []
  if (!cfg.length) return {}
  
  const first = cfg[0]
  const dims: string[] = (first?.dimensions || []).map((d: any) => String(d).toLowerCase())
  if (!dims.length) return {}
  
  const indicators = dims.map(d => ({ name: d.toUpperCase() }))
  const colors = getColors()
  const seriesData: any[] = []
  
  cfg.forEach((s: any, i: number) => {
    const name = s?.name
    const values = dims.map(d => {
      const row = rows.find((r: any) => String(r[(s?.key || 'name').toLowerCase()]) === name) || rows[0]
      const v = Number(row?.[d])
      return Number.isNaN(v) ? 0 : v
    })
    seriesData.push({ name, value: values, itemStyle: { color: resolveColorInput(colors[i % colors.length]) } })
  })
  
  return {
    legend: { show: props.view?.legendVisible ?? true, bottom: '1%', textStyle: { color: tokens.value?.legend?.textColor } },
    radar: { indicator: indicators, shape: 'circle', center: ['50%', '55%'], radius: '65%' },
    series: [{ type: 'radar', data: seriesData }]
  }
}

// Infer default series from data when encoding is absent
function inferDefaultSeries(type: string, data: any): any[] | null {
  try {
    const rows: any[] = Array.isArray(data?.rows) ? data.rows : []
    const columns: any[] = Array.isArray(data?.columns) ? data.columns : []
    if (!rows.length && !columns.length) return null
    
    const sample = rows[0] || {}
    const keys = columns.length ? columns.map((c: any) => c.field || c.colId || c.headerName).filter(Boolean) : Object.keys(sample)
    if (!Array.isArray(keys) || !keys.length) return null
    
    const lower = (s: any) => String(s || '').toLowerCase()
    const isNumeric = (v: any) => v != null && !Number.isNaN(Number(v))
    
    const commonKeyNames = ['label', 'name', 'category', 'genre', 'type']
    const commonValNames = ['value', 'count', 'total', 'amount', 'revenue']
    
    let keyField = keys.find(k => commonKeyNames.includes(lower(k))) || null
    let valueField = keys.find(k => commonValNames.includes(lower(k))) || null
    
    if (!keyField) keyField = keys.find(k => typeof sample[k] === 'string') || keys.find(k => !isNumeric(sample[k])) || keys[0] || null
    if (!valueField) valueField = keys.find(k => isNumeric(sample[k])) || (keys.length > 1 ? keys[1] : null)
    
    if (!keyField || !valueField) return null
    
    if (type === 'pie_chart') return [{ name: 'Series', key: keyField, value: valueField }]
    if (type === 'bar_chart' || type === 'line_chart' || type === 'area_chart') return [{ name: 'Series', key: keyField, value: valueField }]
    return null
  } catch { return null }
}

function buildOptions() {
  isLoading.value = true
  chartOptions.value = {}
  
  // Merge view.encoding or view.view into data_model
  let dm = (() => {
    const base = props.data_model || {}
    const viewV2 = props.view?.view
    const enc: any = (props.view as any)?.encoding || null
    
    // Handle v2 schema overrides
    if (viewV2) {
      const out: any = { ...base }
      if (viewV2.x || viewV2.y) {
        const yFields = Array.isArray(viewV2.y)
          ? viewV2.y.filter(Boolean)
          : viewV2.y
            ? [viewV2.y]
            : []
        if (yFields.length) {
          out.series = yFields.map((field: string, idx: number) => {
            const existing = Array.isArray(base.series) ? base.series[idx] : null
            return {
              name: viewV2.seriesStyles?.[idx]?.label || existing?.name || `Series ${idx + 1}`,
              key: viewV2.x || existing?.key,
              value: field || existing?.value
            }
          })
        } else if (viewV2.x && Array.isArray(out.series)) {
          out.series = out.series.map((s: any) => ({ ...s, key: viewV2.x }))
        }
      }
      if (viewV2.groupBy) out.group_by = viewV2.groupBy
      if (typeof viewV2.horizontal === 'boolean') out.horizontal = viewV2.horizontal
      return out
    }
    
    if (!enc) return base
    const out: any = { ...base }
    
    if (Array.isArray(enc.series) && enc.series.length > 0) {
      const t = normalizeType((props.view as any)?.type || base.type)
      let series = enc.series.map((s: any) => ({ ...s }))
      if (t === 'bar_chart' || t === 'line_chart' || t === 'area_chart') {
        if (enc.category) series = series.map((s: any) => ({ ...s, key: enc.category }))
      }
      if (t === 'pie_chart') {
        if (enc.category) series = series.map((s: any) => ({ ...s, key: s.key || enc.category }))
      }
      out.series = series
      return out
    }
    
    if (enc.category && enc.value) {
      out.series = [{ name: enc.name, key: enc.category, value: enc.value }]
      return out
    }
    if ((enc.x || enc.key) && (enc.y || enc.value)) {
      out.series = [{ name: enc.name, x: enc.x || enc.key, y: enc.y || enc.value }]
      return out
    }
    if (enc.x && enc.y && enc.value) {
      out.series = [{ name: enc.name, x: enc.x, y: enc.y, value: enc.value }]
      return out
    }
    if (enc.open && enc.close && enc.low && enc.high) {
      out.series = [{ name: enc.name, key: enc.category || enc.key, open: enc.open, close: enc.close, low: enc.low, high: enc.high }]
      return out
    }
    if (Array.isArray(enc.dimensions) && enc.dimensions.length) {
      out.series = [{ name: enc.name || 'Series', key: enc.category || enc.key || 'name', dimensions: enc.dimensions }]
      return out
    }
    return out
  })()
  
  // Infer series if missing
  if (!dm || !Array.isArray(dm.series) || dm.series.length === 0) {
    const t = normalizeType((props.view?.view?.type || props.view as any)?.type || (dm as any)?.type)
    const inferred = inferDefaultSeries(t, props.data)
    if (inferred && inferred.length) dm = { ...(dm || {}), type: t, series: inferred }
  }
  
  const rows = normalizeRows(props.data?.rows)
  if (!dm || (!rows.length && normalizeType((props.view?.view?.type || props.view as any)?.type || dm.type) !== 'table')) {
    isLoading.value = false
    chartKey.value++
    return
  }
  
  const t = normalizeType((props.view?.view?.type || props.view as any)?.type || dm.type)
  const base = getBaseOptions()
  let specific: EChartsOption = {}
  
  try {
    if (t === 'pie_chart') specific = buildPieOptions(rows, dm)
    else if (t === 'bar_chart' || t === 'line_chart' || t === 'area_chart') specific = buildCartesianOptions(rows, dm)
    else if (t === 'scatter_plot') specific = buildScatterOptions(rows, dm)
    else if (t === 'heatmap') specific = buildHeatmapOptions(rows, dm)
    else if (t === 'candlestick') specific = buildCandlestickOptions(rows, dm)
    else if (t === 'treemap') specific = buildTreemapOptions(rows, dm)
    else if (t === 'radar_chart') specific = buildRadarOptions(rows, dm)
    else specific = { title: { ...base.title, text: 'Unsupported Chart Type' } }
    
    chartOptions.value = { ...base, ...specific }
  } catch (e) {
    chartOptions.value = { title: { text: 'Error Building Chart' } }
  } finally {
    isLoading.value = false
    chartKey.value++
  }
}

watch(() => [props.step?.id, props.data?.rows, props.data_model, props.view, tokens.value], () => {
  buildOptions()
}, { deep: true, immediate: true })
</script>

<style scoped>
.chart { width: 100%; min-height: 100px; height: 100%; }
</style>
