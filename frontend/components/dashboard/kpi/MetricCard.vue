<template>
  <div class="h-full w-full flex flex-col rounded-lg overflow-hidden" :style="cardStyle">
    <!-- Top section: Metric display -->
    <div class="flex-1 flex flex-col p-5">
      <!-- Title -->
      <div v-if="title" class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-4 truncate">
        {{ title }}
      </div>
      
      <!-- Main Value -->
      <div class="mb-2">
        <span class="text-4xl font-bold tracking-tight" :style="{ color: valueColor }">
          {{ formattedValue }}
        </span>
      </div>
      
      <!-- Comparison text with arrow -->
      <div v-if="showTrend && comparisonValue !== null" class="flex items-center gap-1">
        <component 
          :is="trendIcon" 
          class="w-4 h-4 flex-shrink-0" 
          :style="{ color: trendColor }" 
        />
        <span class="text-sm font-medium" :style="{ color: trendColor }">
          {{ formattedComparison }}
        </span>
        <span v-if="comparisonLabel" class="text-sm text-gray-400">
          {{ comparisonLabel }}
        </span>
      </div>
      
      <!-- Subtitle / Description -->
      <div v-if="subtitle" class="text-sm text-gray-400 mt-2 truncate">
        {{ subtitle }}
      </div>
    </div>
    
    <!-- Sparkline section - full width, no padding -->
    <div v-if="sparklineEnabled" class="w-full" :style="{ height: `${sparklineHeight}px` }">
      <EChartsVisual
        :data="props.data"
        :data_model="sparklineDataModel"
        :view="sparklineView"
        :reportThemeName="reportThemeName"
        :reportOverrides="reportOverrides"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, toRefs, h } from 'vue'
import { useDashboardTheme } from '../composables/useDashboardTheme'
import EChartsVisual from '../charts/EChartsVisual.vue'

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

// Extract view config (v2 schema)
const viewConfig = computed(() => props.view?.view || {})

const title = computed(() => 
  viewConfig.value?.title || props.step?.title || props.widget?.title || ''
)

const subtitle = computed(() => 
  viewConfig.value?.subtitle || viewConfig.value?.description || ''
)

// Parse a raw or display-formatted numeric cell ("1234", "₪29,134,139",
// "4,125.04", "45.2%") into a number. Returns null for anything else (dates,
// labels, mixed text). parseFloat must never be used on cell values here: it
// silently truncates at the first separator (parseFloat("29,134,139") === 29).
function parseNumericLike(val: any): number | null {
  if (typeof val === 'number') return Number.isFinite(val) ? val : null
  if (typeof val !== 'string') return null
  let s = val.trim()
  if (!s) return null
  s = s.replace(/[₪$€£¥,\s  ]/g, '')
  if (s.endsWith('%')) s = s.slice(0, -1)
  if (!/^[+-]?\d+(\.\d+)?$/.test(s)) return null
  const n = Number(s)
  return Number.isFinite(n) ? n : null
}

// Get value column from view (validated against the data), else auto-detect a
// numeric-looking column. Never falls back to an arbitrary first column — a
// label column rendered as the metric ("תאריך") is worse than showing '—'.
const valueColumn = computed(() => {
  const rows = props.data?.rows
  const firstRow = (Array.isArray(rows) && rows.length > 0) ? rows[0] : null
  const keys = Object.keys(firstRow || {})

  const v = viewConfig.value?.value
  if (v) {
    const wanted = String(v).toLowerCase()
    // Only trust the configured column if it exists in the data; otherwise
    // (hallucinated/renamed column) fall through to auto-detection.
    if (!firstRow || keys.some(k => k.toLowerCase() === wanted)) return wanted
  }

  if (!firstRow) return null
  // Prefer truly numeric cells, then numeric-looking strings ("₪1,234").
  for (const k of keys) {
    if (typeof firstRow[k] === 'number') return k.toLowerCase()
  }
  for (const k of keys) {
    if (typeof firstRow[k] === 'string' && parseNumericLike(firstRow[k]) !== null) return k.toLowerCase()
  }
  // A single-column result is a deliberate single value even if non-numeric.
  if (keys.length === 1) return keys[0].toLowerCase()
  return null
})

const comparisonColumn = computed(() => {
  const c = viewConfig.value?.comparison
  return c ? c.toLowerCase() : null
})

// Optional aggregation function from view schema. When absent the metric is
// read from the first row (legacy behaviour). When set, aggregate across all
// rows so granular source data renders a correct card value.
type MetricAggregationFn = 'sum' | 'avg' | 'count' | 'min' | 'max'
const aggregationFn = computed<MetricAggregationFn | null>(() => {
  const fn = viewConfig.value?.aggregation
  return (fn as MetricAggregationFn) || null
})

function aggregateNumbers(values: number[], fn?: MetricAggregationFn | null): number | null {
  if (!values.length) return null
  if (!fn) return values[0]
  switch (fn) {
    case 'sum': return values.reduce((a, b) => a + b, 0)
    case 'avg': return values.reduce((a, b) => a + b, 0) / values.length
    case 'count': return values.length
    case 'min': return values.reduce((a, b) => (a < b ? a : b))
    case 'max': return values.reduce((a, b) => (a > b ? a : b))
    default: return values[0]
  }
}

// Extract raw values
const rawValue = computed(() => {
  const rows = props.data?.rows
  if (!Array.isArray(rows) || rows.length === 0) return null

  const col = valueColumn.value
  const fn = aggregationFn.value

  if (col && fn) {
    // `count` is row-cardinality, not a numeric reduction. Counting only
    // parseable numbers would undercount string/boolean columns, so special-
    // case it to non-null occurrences of the selected column.
    if (fn === 'count') {
      let n = 0
      for (const row of rows) {
        if (!row) continue
        const key = Object.keys(row).find(k => k.toLowerCase() === col)
        if (!key) continue
        const v = row[key]
        if (v !== null && v !== undefined && v !== '') n += 1
      }
      return n
    }
    const values: number[] = []
    for (const row of rows) {
      if (!row) continue
      const key = Object.keys(row).find(k => k.toLowerCase() === col)
      if (!key) continue
      const num = parseNumericLike(row[key])
      if (num !== null) values.push(num)
    }
    return aggregateNumbers(values, fn)
  }

  const firstRow = rows[0]
  if (!firstRow) return null

  if (col) {
    const key = Object.keys(firstRow).find(k => k.toLowerCase() === col)
    if (key) {
      // Melted label/value table with no row selector (no filter narrowed the
      // rows, no aggregation): rows[0] is an arbitrary metric's row — showing
      // its value would be silently wrong. '—' is the honest render.
      if (looksMeltedWithoutRowSelector(rows, col)) return null
      return firstRow[key]
    }
  }

  // No resolvable value column: show '—' rather than an arbitrary first cell
  // (which is how a melted table's label ended up rendered as the metric).
  return null
})

const TIME_NAME_RE = /(date|time|day|month|week|year|period|timestamp|תאריך|חודש|שנה|יום|שבוע)/i
const DATE_STR_RE = /^\d{4}-\d{1,2}-\d{1,2}([ T].*)?$|^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$|^\d{4}[./]\d{1,2}[./]\d{1,2}$/

// A melted KPI table is a label column next to the value column, one row per
// metric ("מדד | ערך"). Time-series data (a date/month column next to the
// measure) is NOT melted — that's the legit "latest value + sparkline" card
// and keeps the legacy rows[0] behaviour.
function looksMeltedWithoutRowSelector(rows: any[], col: string): boolean {
  if (!Array.isArray(rows) || rows.length < 2) return false
  const keys = Object.keys(rows[0] || {})
  const others = keys.filter(k => k.toLowerCase() !== col)
  if (others.length !== 1) return false
  const label = others[0]
  if (TIME_NAME_RE.test(label)) return false
  const cells = rows.slice(0, 50).map(r => r?.[label]).filter(v => v !== null && v !== undefined)
  if (!cells.length) return false
  const allNonNumeric = cells.every(v => parseNumericLike(v) === null)
  const mostlyDates = cells.filter(v => typeof v === 'string' && DATE_STR_RE.test(v.trim())).length / cells.length >= 0.8
  const mostlyDistinct = new Set(cells.map(v => String(v))).size / cells.length >= 0.9
  return allNonNumeric && !mostlyDates && mostlyDistinct
}

const comparisonValue = computed(() => {
  if (!comparisonColumn.value) return null
  
  const rows = props.data?.rows
  if (!Array.isArray(rows) || rows.length === 0) return null
  
  const firstRow = rows[0]
  if (!firstRow) return null
  
  const key = Object.keys(firstRow).find(k => k.toLowerCase() === comparisonColumn.value)
  return key ? firstRow[key] : null
})

// Formatting
const formatType = computed(() => viewConfig.value?.format || 'number')
const comparisonFormatType = computed(() => viewConfig.value?.comparisonFormat || 'percent')
const prefix = computed(() => viewConfig.value?.prefix || '')
const suffix = computed(() => viewConfig.value?.suffix || '')
const comparisonLabel = computed(() => viewConfig.value?.comparisonLabel || '')
const invertTrend = computed(() => viewConfig.value?.invertTrend === true)

function formatNumber(val: any, format?: string): string {
  if (val === null || val === undefined) return '—'

  const parsed = parseNumericLike(val)
  const num = parsed === null ? NaN : parsed
  if (isNaN(num)) return String(val)
  
  const fmt = format || formatType.value
  
  switch (fmt) {
    case 'currency':
      return new Intl.NumberFormat('en-US', { 
        style: 'currency', 
        currency: 'USD',
        minimumFractionDigits: 0,
        maximumFractionDigits: 2
      }).format(num)
    
    case 'percent':
      return new Intl.NumberFormat('en-US', { 
        style: 'percent',
        minimumFractionDigits: 0,
        maximumFractionDigits: 1
      }).format(num / 100)
    
    case 'compact':
      return new Intl.NumberFormat('en-US', { 
        notation: 'compact',
        maximumFractionDigits: 1
      }).format(num)
    
    default:
      return new Intl.NumberFormat('en-US', {
        minimumFractionDigits: 0,
        maximumFractionDigits: 2
      }).format(num)
  }
}

const formattedValue = computed(() => {
  const formatted = formatNumber(rawValue.value)
  if (formatted === '—') return formatted
  return `${prefix.value}${formatted}${suffix.value}`
})

const formattedComparison = computed(() => {
  if (comparisonValue.value === null) return ''
  const parsed = parseNumericLike(comparisonValue.value)
  const num = parsed === null ? NaN : parsed
  if (isNaN(num)) return ''
  
  const sign = num >= 0 ? '+' : ''
  
  // Use comparisonFormat for the trend value
  if (comparisonFormatType.value === 'percent') {
    return `${sign}${num.toFixed(1)}%`
  }
  if (comparisonFormatType.value === 'compact') {
    return `${sign}${formatNumber(num, 'compact')}`
  }
  return `${sign}${formatNumber(num, 'number')}`
})

// Trend direction
const trendDirection = computed(() => {
  // Explicit from view
  const explicit = viewConfig.value?.trendDirection
  if (explicit) return explicit
  
  // Infer from comparison value
  if (comparisonValue.value !== null) {
    const num = parseNumericLike(comparisonValue.value)
    if (num !== null) {
      if (num > 0) return 'up'
      if (num < 0) return 'down'
      return 'flat'
    }
  }
  return null
})

const trendIndicator = computed(() => viewConfig.value?.trendIndicator || 'arrow')
const showTrend = computed(() => trendIndicator.value !== 'none' && trendDirection.value)

// Icons - diagonal arrows for better visual
const ArrowUp = {
  render: () => h('svg', { 
    xmlns: 'http://www.w3.org/2000/svg', 
    viewBox: '0 0 20 20', 
    fill: 'currentColor' 
  }, [
    h('path', { 
      'fill-rule': 'evenodd',
      d: 'M5.22 14.78a.75.75 0 001.06 0l7.22-7.22v5.69a.75.75 0 001.5 0v-7.5a.75.75 0 00-.75-.75h-7.5a.75.75 0 000 1.5h5.69l-7.22 7.22a.75.75 0 000 1.06z',
      'clip-rule': 'evenodd'
    })
  ])
}

const ArrowDown = {
  render: () => h('svg', { 
    xmlns: 'http://www.w3.org/2000/svg', 
    viewBox: '0 0 20 20', 
    fill: 'currentColor' 
  }, [
    h('path', { 
      'fill-rule': 'evenodd',
      d: 'M5.22 5.22a.75.75 0 011.06 0L13.5 12.44V6.75a.75.75 0 011.5 0v7.5a.75.75 0 01-.75.75h-7.5a.75.75 0 010-1.5h5.69L5.22 6.28a.75.75 0 010-1.06z',
      'clip-rule': 'evenodd'
    })
  ])
}

const ArrowFlat = {
  render: () => h('svg', { 
    xmlns: 'http://www.w3.org/2000/svg', 
    viewBox: '0 0 20 20', 
    fill: 'currentColor' 
  }, [
    h('path', { 
      'fill-rule': 'evenodd',
      d: 'M2 10a.75.75 0 01.75-.75h12.59l-2.1-1.95a.75.75 0 111.02-1.1l3.5 3.25a.75.75 0 010 1.1l-3.5 3.25a.75.75 0 11-1.02-1.1l2.1-1.95H2.75A.75.75 0 012 10z',
      'clip-rule': 'evenodd'
    })
  ])
}

const trendIcon = computed(() => {
  switch (trendDirection.value) {
    case 'up': return ArrowUp
    case 'down': return ArrowDown
    default: return ArrowFlat
  }
})

// Colors - respect invertTrend for metrics where down is good
const trendColor = computed(() => {
  const dir = trendDirection.value
  const invert = invertTrend.value
  
  if (dir === 'up') {
    return invert ? '#dc2626' : '#16a34a' // red if inverted, else green
  }
  if (dir === 'down') {
    return invert ? '#16a34a' : '#dc2626' // green if inverted, else red
  }
  return '#6b7280' // gray for flat
})

const valueColor = computed(() => {
  // Use palette primary or default
  const palette = viewConfig.value?.palette
  if (palette?.colors?.[0]) return palette.colors[0]
  return tokens.value?.textColor || '#111827'
})

// Card styling - default to transparent/no border (like EChartsVisual)
// Only add styling when explicitly set via view.style
const cardStyle = computed(() => {
  const style = (props.view?.style as any) || {}
  const bg = style.cardBackground
  const border = style.cardBorder
  const shadow = style.cardShadow
  
  const out: Record<string, any> = {
    backgroundColor: 'transparent',
    border: 'none',
    boxShadow: 'none',
  }
  
  // Only override if explicitly set
  if (bg) out.backgroundColor = bg
  if (border && border !== 'none' && typeof border === 'string' && border.trim().length) {
    out.border = `1px solid ${border}`
  }
  if (shadow && shadow !== 'none') {
    out.boxShadow = shadow
  }
  
  return out
})

// --- Sparkline Configuration ---
const sparklineConfig = computed(() => viewConfig.value?.sparkline || {})
const sparklineEnabled = computed(() => sparklineConfig.value?.enabled === true)
const sparklineHeight = computed(() => sparklineConfig.value?.height || 64)

// Determine sparkline columns
const sparklineValueColumn = computed(() => {
  return sparklineConfig.value?.column || valueColumn.value
})

const sparklineXColumn = computed(() => {
  // Try to find a date/time column if not specified
  if (sparklineConfig.value?.xColumn) {
    return sparklineConfig.value.xColumn
  }
  
  // Auto-detect: look for common date column names
  const rows = props.data?.rows
  if (!Array.isArray(rows) || rows.length === 0) return null
  
  const firstRow = rows[0]
  const keys = Object.keys(firstRow || {})
  const datePatterns = ['date', 'time', 'day', 'month', 'week', 'period', 'timestamp']
  
  for (const k of keys) {
    const lower = k.toLowerCase()
    if (datePatterns.some(p => lower.includes(p))) {
      return k
    }
  }
  
  // Fallback to first non-numeric column
  for (const k of keys) {
    if (typeof firstRow[k] !== 'number') return k
  }
  
  return keys[0]
})

// View config for EChartsVisual - stripped down (no axes, grid, legend)
const sparklineView = computed(() => {
  const color = sparklineConfig.value?.color || tokens.value?.palette?.[0] || '#6b7280'
  const chartType = sparklineConfig.value?.type || 'area'
  
  return {
    view: {
      type: chartType === 'line' ? 'line_chart' : 'area_chart',
      x: sparklineXColumn.value,
      y: [sparklineValueColumn.value],
      axisX: { show: false },
      axisY: { show: false },
      legend: { show: false },
      showGrid: false,
      smooth: true,
      palette: { custom: [color] },
      // Remove all internal chart padding
      grid: { left: 0, right: 0, top: 0, bottom: 0, containLabel: false },
      tooltip: false,
      sparkline: true
    },
    style: {
      cardBackground: 'transparent',
      cardBorder: 'none'
    }
  }
})

// Data model for the sparkline chart
const sparklineDataModel = computed(() => ({
  type: sparklineConfig.value?.type === 'line' ? 'line_chart' : 'area_chart',
  series: [{
    name: 'Sparkline',
    key: sparklineXColumn.value,
    value: sparklineValueColumn.value
  }]
}))
</script>

<style scoped>
/* Ensure sparkline fills width properly */
:deep(.chart) {
  width: 100% !important;
}
</style>
