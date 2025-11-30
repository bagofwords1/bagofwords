<template>
  <div class="h-full w-full flex flex-col rounded-lg overflow-hidden" :style="cardStyle">
    <!-- Top section: Metric display -->
    <div class="flex-1 flex flex-col p-5">
      <!-- Title -->
      <div v-if="title" class="text-sm font-medium text-gray-500 mb-4 truncate">
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

// Get value column from view or first numeric column
const valueColumn = computed(() => {
  const v = viewConfig.value?.value
  if (v) return v.toLowerCase()
  
  // Fallback: first column from data
  const rows = props.data?.rows
  if (Array.isArray(rows) && rows.length > 0) {
    const firstRow = rows[0]
    const keys = Object.keys(firstRow || {})
    // Prefer numeric columns
    for (const k of keys) {
      if (typeof firstRow[k] === 'number') return k.toLowerCase()
    }
    return keys[0]?.toLowerCase()
  }
  return null
})

const comparisonColumn = computed(() => {
  const c = viewConfig.value?.comparison
  return c ? c.toLowerCase() : null
})

// Extract raw values
const rawValue = computed(() => {
  const rows = props.data?.rows
  if (!Array.isArray(rows) || rows.length === 0) return null
  
  const firstRow = rows[0]
  if (!firstRow) return null
  
  if (valueColumn.value) {
    // Case-insensitive lookup
    const key = Object.keys(firstRow).find(k => k.toLowerCase() === valueColumn.value)
    if (key) return firstRow[key]
  }
  
  // Fallback to first value
  return Object.values(firstRow)[0]
})

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
  
  const num = typeof val === 'number' ? val : parseFloat(String(val))
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
  const num = typeof comparisonValue.value === 'number' 
    ? comparisonValue.value 
    : parseFloat(String(comparisonValue.value))
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
    const num = typeof comparisonValue.value === 'number' 
      ? comparisonValue.value 
      : parseFloat(String(comparisonValue.value))
    if (!isNaN(num)) {
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
