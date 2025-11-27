<template>
  <div class="h-full w-full flex flex-col justify-center p-4" :style="wrapperStyle">
    <!-- Title -->
    <div v-if="title" class="text-xs font-medium text-gray-500 mb-1 truncate">
      {{ title }}
    </div>
    
    <!-- Main Value -->
    <div class="flex items-baseline gap-2">
      <span class="text-3xl font-bold tracking-tight" :style="{ color: valueColor }">
        {{ formattedValue }}
      </span>
      
      <!-- Trend Indicator -->
      <div v-if="showTrend && trendDirection" class="flex items-center gap-0.5">
        <component :is="trendIcon" class="w-4 h-4" :style="{ color: trendColor }" />
        <span v-if="comparisonValue !== null" class="text-sm font-medium" :style="{ color: trendColor }">
          {{ formattedComparison }}
        </span>
      </div>
    </div>
    
    <!-- Subtitle / Description -->
    <div v-if="subtitle" class="text-xs text-gray-400 mt-1 truncate">
      {{ subtitle }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, toRefs, h } from 'vue'
import { useDashboardTheme } from '../composables/useDashboardTheme'

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
const prefix = computed(() => viewConfig.value?.prefix || '')
const suffix = computed(() => viewConfig.value?.suffix || '')

function formatNumber(val: any): string {
  if (val === null || val === undefined) return '—'
  
  const num = typeof val === 'number' ? val : parseFloat(String(val))
  if (isNaN(num)) return String(val)
  
  switch (formatType.value) {
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
  if (formatType.value === 'percent') {
    return `${sign}${num.toFixed(1)}%`
  }
  return `${sign}${formatNumber(num)}`
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

// Icons
const ArrowUp = {
  render: () => h('svg', { 
    xmlns: 'http://www.w3.org/2000/svg', 
    viewBox: '0 0 20 20', 
    fill: 'currentColor' 
  }, [
    h('path', { 
      'fill-rule': 'evenodd',
      d: 'M10 17a.75.75 0 01-.75-.75V5.612L5.29 9.77a.75.75 0 01-1.08-1.04l5.25-5.5a.75.75 0 011.08 0l5.25 5.5a.75.75 0 11-1.08 1.04l-3.96-4.158V16.25A.75.75 0 0110 17z',
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
      d: 'M10 3a.75.75 0 01.75.75v10.638l3.96-4.158a.75.75 0 111.08 1.04l-5.25 5.5a.75.75 0 01-1.08 0l-5.25-5.5a.75.75 0 111.08-1.04l3.96 4.158V3.75A.75.75 0 0110 3z',
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
      d: 'M2 10a.75.75 0 01.75-.75h14.5a.75.75 0 010 1.5H2.75A.75.75 0 012 10z',
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

// Colors
const trendColor = computed(() => {
  switch (trendDirection.value) {
    case 'up': return '#16a34a' // green-600
    case 'down': return '#dc2626' // red-600
    default: return '#6b7280' // gray-500
  }
})

const valueColor = computed(() => {
  // Use palette primary or default
  const palette = viewConfig.value?.palette
  if (palette?.colors?.[0]) return palette.colors[0]
  return tokens.value?.textColor || '#111827'
})

const wrapperStyle = computed(() => {
  const style = (props.view?.style as any) || {}
  return {
    backgroundColor: style.cardBackground || tokens.value?.background || 'transparent'
  }
})
</script>

<style scoped>
</style>

