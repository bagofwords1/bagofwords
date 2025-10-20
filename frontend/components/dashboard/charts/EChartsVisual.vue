<template>
  <div class="h-full w-full" :style="wrapperStyle">
    <div v-if="!isLoading && chartOptions && Object.keys(chartOptions).length > 0 && (data?.rows?.length || 0) > 0" class="h-full">
      <VChart :key="chartKey" class="chart" :option="chartOptions" autoresize :loading="isLoading" />
    </div>
    <div v-else-if="isLoading">Loading Chart...</div>
    <div v-else-if="!(data?.rows?.length > 0)">No data to display.</div>
    <div v-else>Chart configuration error or unsupported type.</div>
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
  // charts
  PieChart,
  BarChart,
  LineChart,
  ScatterChart,
  HeatmapChart,
  CandlestickChart,
  TreemapChart,
  RadarChart,
  // components
  TitleComponent,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  VisualMapComponent,
  DataZoomComponent,
  MarkLineComponent,
  MarkPointComponent,
  AriaComponent,
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
// Pass reactive refs so tokens update when props change (e.g., theme switch)
const { tokens } = useDashboardTheme(reportThemeName, reportOverrides, props.view || null)

type EChartsOption = Record<string, any>

const isLoading = ref(false)
const chartOptions = ref<EChartsOption>({})
const chartKey = ref(0)

// Card-level wrapper style (not the chart canvas)
const wrapperStyle = computed(() => {
  const style = (props.view?.style as any) || {}
  const bg = style.cardBackground
  const border = style.cardBorder
  const out: Record<string, any> = {}
  if (bg) out.backgroundColor = bg
  if (border === 'none') out.border = 'none'
  else if (typeof border === 'string' && border.trim().length) out.border = `1px solid ${border}`
  return out
})

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

function getBaseOptions(): EChartsOption {
  const titleVisible = props.view?.titleVisible ?? true
  const xVisible = props.view?.xAxisVisible ?? true
  const yVisible = props.view?.yAxisVisible ?? true
  const legendVisible = props.view?.legendVisible ?? false
  // When axes are hidden, avoid reserving space for labels
  const containLabel = xVisible || yVisible
  // Use minimal paddings; eliminate left/right to maximize width
  const topPad: number | string = titleVisible ? 40 : 2
  const bottomPad: number | string = legendVisible ? 24 : 2
  const leftPad: number | string = 0
  const rightPad: number | string = 0
  
  return {
    // Base on theme tokens, allow view to override later via specific fields
    color: undefined,
    // Prefer explicit view style over theme token so editor changes take effect
    backgroundColor: (props.view?.style as any)?.backgroundColor || tokens.value?.background || (props.view?.options as any)?.backgroundColor,
    title: titleVisible ? {
      text: (props.step?.title || props.widget?.title || 'Chart'),
      left: 'center',
      top: 5,
      textStyle: {
        // Prefer explicit title color override, then theme
        color: (props.view?.style as any)?.titleColor || tokens.value?.textColor,
        fontFamily: tokens.value?.headingFontFamily || tokens.value?.fontFamily,
        fontWeight: (props.view?.style as any)?.titleWeight || 700,
        fontSize: (props.view?.style as any)?.titleSize || 18
      }
    } : { show: false },
    grid: { containLabel, left: leftPad, right: rightPad, bottom: bottomPad, top: topPad },
    legend: { show: legendVisible, left: 'center', bottom: 0, textStyle: { color: tokens.value?.legend?.textColor } },
    tooltip: { trigger: 'item', confine: true, ...(tokens.value?.tooltip || {}) },
    series: []
  }
}

function buildPieOptions(rows: any[], dm: any): EChartsOption {
  const cfg = dm?.series?.[0] || {}
  if (!cfg?.key || !cfg?.value) return {}
  const data = rows
    .map((r: any) => ({ name: r[cfg.key.toLowerCase()], value: Number(r[cfg.value.toLowerCase()]) }))
    .filter((d: any) => d.name != null && !Number.isNaN(d.value))
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    series: [{ name: cfg.name, type: 'pie', radius: ['40%', '70%'], center: ['50%', '60%'], data }]
  }
}

function buildCartesianOptions(rows: any[], dm: any): EChartsOption {
  // Prefer explicit view.type over data_model.type for determining cartesian variant
  const t = normalizeType((props.view as any)?.type || dm?.type)
  const variant = props.view?.variant || (t === 'area_chart' ? 'area' : undefined)
  const chartType = t === 'line_chart' || variant === 'area' ? 'line' : 'bar'
  const categoryKey = dm?.series?.[0]?.key?.toLowerCase()
  if (!categoryKey) return {}
  const categories = Array.from(new Set(rows.map((r: any) => String(r[categoryKey] ?? ''))))
  const series = (dm?.series || [])
    .map((s: any) => {
      const valueKey = s?.value?.toLowerCase()
      if (!valueKey) return null
      const data = categories.map(cat => {
        const row = rows.find((r: any) => String(r[categoryKey] ?? '') === cat)
        const v = row ? Number(row[valueKey]) : null
        return Number.isNaN(v as number) ? null : v
      })
      const base: any = { name: s.name, type: chartType, data }
      if (chartType === 'line' && variant === 'area') base.areaStyle = {}
      if (chartType === 'line' && props.view?.variant === 'smooth') base.smooth = true
      return base
    })
    .filter(Boolean)

  const axisColors = { ...(tokens.value?.axis || {}), ...((props.view?.style as any)?.axis || {}) }
  const xVisible = props.view?.xAxisVisible ?? true
  const yVisible = props.view?.yAxisVisible ?? true

  // Build x-axis label configuration
  const xAxisLabel: any = { color: axisColors.xLabelColor }
  
  // Apply x-axis label controls from view, with theme defaults as fallback
  const viewInterval = props.view?.xAxisLabelInterval
  const themeInterval = tokens.value?.axis?.xLabelInterval
  if (viewInterval !== null && viewInterval !== undefined) {
    xAxisLabel.interval = viewInterval
  } else if (themeInterval !== null && themeInterval !== undefined && themeInterval !== 'auto') {
    xAxisLabel.interval = themeInterval
  }
  
  const viewRotate = props.view?.xAxisLabelRotate
  const themeRotate = tokens.value?.axis?.xLabelRotate
  if (viewRotate !== null && viewRotate !== undefined) {
    xAxisLabel.rotate = viewRotate
  } else if (themeRotate !== null && themeRotate !== undefined) {
    xAxisLabel.rotate = themeRotate
  }

  return {
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category', data: categories, name: dm?.series?.[0]?.key || 'Categories',
      show: xVisible,
      axisLabel: xAxisLabel,
      axisLine: { lineStyle: { color: axisColors.xLineColor } },
      splitLine: (() => {
        const explicit = (props.view as any)?.showGridLines
        const show = explicit === true
        return show ? { show: true, lineStyle: { color: axisColors.gridLineColor || '#e5e7eb' } } : { show: false }
      })()
    },
    yAxis: {
      type: 'value', name: 'Values', show: yVisible,
      axisLabel: { color: axisColors.yLabelColor },
      axisLine: { lineStyle: { color: axisColors.yLineColor } },
      splitLine: (() => {
        const explicit = (props.view as any)?.showGridLines
        const show = explicit === true
        return show ? { show: true, lineStyle: { color: axisColors.gridLineColor || '#e5e7eb' } } : { show: false }
      })()
    },
    legend: { show: props.view?.legendVisible ?? false, textStyle: { color: tokens.value?.legend?.textColor } },
    series
  }
}

function buildScatterOptions(rows: any[], dm: any): EChartsOption {
  const s = dm?.series?.[0] || {}
  const xKey = (s?.x || s?.key || '').toLowerCase()
  const yKey = (s?.y || s?.value || '').toLowerCase()
  if (!xKey || !yKey) return {}
  const data = rows
    .map((r: any) => [Number(r[xKey]), Number(r[yKey])])
    .filter((d: any[]) => !d.some(v => Number.isNaN(v)))

  const axisColors = props.view?.style?.axis || tokens.value?.axis || {}
  const xVisible = props.view?.xAxisVisible ?? true
  const yVisible = props.view?.yAxisVisible ?? true

  return {
    tooltip: { trigger: 'item' },
    xAxis: { type: 'value', name: s?.x || s?.key || 'X', show: xVisible, axisLabel: { color: axisColors.xLabelColor }, axisLine: { lineStyle: { color: axisColors.xLineColor } } },
    yAxis: { type: 'value', name: s?.y || s?.value || 'Y', show: yVisible, axisLabel: { color: axisColors.yLabelColor }, axisLine: { lineStyle: { color: axisColors.yLineColor } } },
    series: [{ type: 'scatter', name: s?.name || 'Scatter', data }]
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
  return {
    tooltip: { position: 'top' },
    xAxis: { type: 'category', data: xCats },
    yAxis: { type: 'category', data: yCats },
    visualMap: { min: 0, max: maxVal, orient: 'horizontal', left: 'center', bottom: '5%' },
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
  const seriesData: any[] = []
  cfg.forEach((s: any) => {
    const name = s?.name
    const values = dims.map(d => {
      const row = rows.find((r: any) => String(r[(s?.key || 'name').toLowerCase()]) === name) || rows[0]
      const v = Number(row?.[d])
      return Number.isNaN(v) ? 0 : v
    })
    seriesData.push({ name, value: values })
  })
  return {
    legend: { show: props.view?.legendVisible ?? true, bottom: '1%', textStyle: { color: tokens.value?.legend?.textColor } },
    radar: { indicator: indicators, shape: 'circle', center: ['50%', '55%'], radius: '65%' },
    series: [{ type: 'radar', data: seriesData }]
  }
}

// Heuristic: infer a minimal series mapping from data when encoding is absent
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
    // Prefer a string/categorical key and a numeric value
    let keyField: string | null = null
    let valueField: string | null = null
    // Try common names first
    const commonKeyNames = ['label', 'name', 'category', 'genre', 'type']
    const commonValNames = ['value', 'count', 'total', 'amount', 'revenue']
    keyField = keys.find(k => commonKeyNames.includes(lower(k))) || null
    valueField = keys.find(k => commonValNames.includes(lower(k))) || null
    // Fallbacks from data content
    if (!keyField) {
      keyField = keys.find(k => typeof sample[k] === 'string') || keys.find(k => !isNumeric(sample[k])) || keys[0] || null
    }
    if (!valueField) {
      valueField = keys.find(k => isNumeric(sample[k])) || (keys.length > 1 ? keys[1] : null)
    }
    if (!keyField || !valueField) return null
    // Build minimal single-series mapping
    if (type === 'pie_chart') {
      return [{ name: 'Series', key: keyField, value: valueField }]
    }
    if (type === 'bar_chart' || type === 'line_chart' || type === 'area_chart') {
      return [{ name: 'Series', key: keyField, value: valueField }]
    }
    return null
  } catch {
    return null
  }
}

function resolveColorInput(input: any): any {
  // Normalize theme palette entries to ECharts LinearGradient or solid color
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

function paletteArray(): any[] {
  // Theme palette as default
  const themePalette = tokens.value?.palette as any
  const theme = Array.isArray(themePalette) && themePalette.length ? themePalette : []
  return theme
}

function firstColorString(c: any, fallback: string): string {
  if (typeof c === 'string') return c
  if (c && c.colorStops && c.colorStops.length) return c.colorStops[0].color || fallback
  return fallback
}

function applyThemeColors(option: EChartsOption, type: string, dm: any) {
  // Start from theme palette
  let pal = paletteArray()
  // If view provides explicit colors, let them override the palette entirely
  const viewColors = (props.view?.options as any)?.colors
  if (Array.isArray(viewColors) && viewColors.length) pal = viewColors
  if (!Array.isArray(option.series)) return
  if (type === 'pie_chart' && option.series[0] && Array.isArray(option.series[0].data)) {
    option.series[0].data = option.series[0].data.map((d: any, i: number) => ({
      ...d,
      itemStyle: { color: resolveColorInput(pal[i % pal.length]) }
    }))
    return
  }
  if (type === 'bar_chart' || type === 'line_chart' || type === 'area_chart' || type === 'scatter_plot') {
    option.series = option.series.map((s: any, i: number) => {
      const color = resolveColorInput(pal[i % pal.length])
      const next = { ...s, itemStyle: { ...(s.itemStyle || {}), color } }
      if (s.type === 'line' && (dm?.type === 'area_chart' || props.view?.variant === 'area')) {
        next.areaStyle = { ...(s.areaStyle || {}), color }
      }
      return next
    })
    return
  }
  if (type === 'heatmap' && option.visualMap) {
    // Heatmap visualMap expects an array of color strings; derive from palette
    const colors = pal.slice(0, 4).map((c: any, idx: number) => firstColorString(c, ['#22d3ee','#38bdf8','#a78bfa','#fbbf24'][idx] || '#22d3ee'))
    option.visualMap.inRange = { ...(option.visualMap.inRange || {}), color: colors }
  }
}

function buildOptions() {
  isLoading.value = true
  chartOptions.value = {}
  // Merge view.encoding into data_model (do not mutate props)
  let dm = (() => {
    const base = props.data_model || {}
    const enc: any = (props.view as any)?.encoding || null
    if (!enc) return base
    const out: any = { ...base }
    // If series provided explicitly, prefer it but normalize missing keys
    if (Array.isArray(enc.series) && enc.series.length > 0) {
      const t = normalizeType((props.view as any)?.type || base.type)
      let series = enc.series.map((s: any) => ({ ...s }))
      // For cartesian charts, always set series key from encoding.category so x-axis reflects current category
      if (t === 'bar_chart' || t === 'line_chart' || t === 'area_chart') {
        if (enc.category) series = series.map((s: any) => ({ ...s, key: enc.category }))
      }
      // For pie, ensure key is present; derive from enc.category if missing
      if (t === 'pie_chart') {
        if (enc.category) series = series.map((s: any) => ({ ...s, key: s.key || enc.category }))
      }
      out.series = series
      return out
    }
    // Common single-series mapping: category + value (+name)
    if (enc.category && enc.value) {
      out.series = [{ name: enc.name, key: enc.category, value: enc.value }]
      return out
    }
    // Scatter: x/y (fallback to key/value)
    if ((enc.x || enc.key) && (enc.y || enc.value)) {
      out.series = [{ name: enc.name, x: enc.x || enc.key, y: enc.y || enc.value }]
      return out
    }
    // Heatmap: x/y/value
    if (enc.x && enc.y && enc.value) {
      out.series = [{ name: enc.name, x: enc.x, y: enc.y, value: enc.value }]
      return out
    }
    // Candlestick: open/close/low/high + key
    if (enc.open && enc.close && enc.low && enc.high) {
      out.series = [{ name: enc.name, key: enc.category || enc.key, open: enc.open, close: enc.close, low: enc.low, high: enc.high }]
      return out
    }
    // Radar: dimensions + name
    if (Array.isArray(enc.dimensions) && enc.dimensions.length) {
      out.series = [{ name: enc.name || 'Series', key: enc.category || enc.key || 'name', dimensions: enc.dimensions }]
      return out
    }
    return out
  })()
  // If no series mapping is available, attempt to infer sensible defaults
  if (!dm || !Array.isArray(dm.series) || dm.series.length === 0) {
    const t = normalizeType((props.view as any)?.type || (dm as any)?.type)
    const inferred = inferDefaultSeries(t, props.data)
    if (inferred && inferred.length) {
      dm = { ...(dm || {}), type: t, series: inferred }
    }
  }
  const rows = normalizeRows(props.data?.rows)
  // For table type, allow empty or any rows; table is rendered outside of this component
  if (!dm || (!rows.length && normalizeType((props.view as any)?.type || dm.type) !== 'table')) {
    isLoading.value = false
    chartKey.value++
    return
  }
  const t = normalizeType((props.view as any)?.type || dm.type)
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
    const merged = { ...base, ...specific }
    applyThemeColors(merged, t, dm)
    chartOptions.value = merged
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


