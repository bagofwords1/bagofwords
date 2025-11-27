<template>
  <div class="h-full w-full flex flex-col justify-center p-4" :style="wrapperStyle">
    <!-- Title -->
    <div v-if="title" class="text-xs font-medium text-gray-500 mb-1 truncate">
      {{ title }}
    </div>
    
    <!-- Main Value -->
    <div class="text-3xl font-bold tracking-tight" :style="{ color: valueColor }">
      {{ formattedValue }}
    </div>
    
    <!-- Subtitle -->
    <div v-if="subtitle" class="text-xs text-gray-400 mt-1 truncate">
      {{ subtitle }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, toRefs } from 'vue'
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

// Get value column from view or first column
const valueColumn = computed(() => {
  const v = viewConfig.value?.value
  if (v) return v.toLowerCase()
  return null
})

// Extract raw value
const rawValue = computed(() => {
  const rows = props.data?.rows
  if (!Array.isArray(rows) || rows.length === 0) return null
  
  const firstRow = rows[0]
  if (!firstRow) return null
  
  if (valueColumn.value) {
    const key = Object.keys(firstRow).find(k => k.toLowerCase() === valueColumn.value)
    if (key) return firstRow[key]
  }
  
  // Fallback to first value
  return Object.values(firstRow)[0]
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

// Colors
const valueColor = computed(() => {
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
