<template>
  <div class="h-full w-full flex flex-col overflow-hidden relative">
    <!-- Filter button (top-right) - hidden when parent CardBlock shows it -->
    <div
      v-if="hasData && reportId && !hideFilter"
      class="absolute top-3 right-4 z-10"
    >
      <VisualizationFilter
        :report-id="reportId"
        :visualization-id="widget.id"
        :rows="widget.last_step?.data?.rows || []"
        :columns="widget.last_step?.data?.columns"
      />
    </div>

    <div v-if="isTable" class="flex-1 min-h-0">
      <component
        :is="tableComp"
        :widget="filteredWidget"
        :step="{ ...(filteredWidget.last_step || {}), data_model: { ...(filteredWidget.last_step?.data_model || {}), type: 'table' } }"
        :view="finalView"
        :reportThemeName="themeName"
        :reportOverrides="reportOverrides"
      />
    </div>
    <div v-else-if="resolvedComp" class="flex-1 min-h-0">
      <component
        :key="`${widget.id}:${themeName}`"
        :is="resolvedComp"
        :widget="filteredWidget"
        :data="filteredWidget.last_step?.data"
        :data_model="filteredWidget.last_step?.data_model"
        :step="filteredWidget.last_step"
        :view="finalView"
        :reportThemeName="themeName"
        :reportOverrides="reportOverrides"
      />
    </div>
    <div v-else-if="widget?.last_step?.type == 'init'" class="flex-1 flex items-center justify-center text-gray-500">
      <SpinnerComponent />
      <span class="ml-2 text-sm">Loading...</span>
    </div>
    <div v-else class="flex-1 flex items-center justify-center text-gray-400 italic text-sm">
      No data or visualization available.
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, ref, onMounted, onUnmounted } from 'vue'
import SpinnerComponent from '@/components/SpinnerComponent.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'
import TableAgGrid from '@/components/dashboard/table/TableAgGrid.vue'
import VisualizationFilter from '@/components/dashboard/VisualizationFilter.vue'
import { evaluateFilters, type FilterGroup } from '~/composables/useSharedFilters'

const props = defineProps<{
  widget: any
  themeName: string
  reportOverrides: any
  reportId?: string  // Required for filtering
  hideFilter?: boolean  // Hide filter when parent (CardBlock) shows it
}>()

// Expose props to template
const widget = computed(() => props.widget)
const themeName = computed(() => props.themeName)
const reportOverrides = computed(() => props.reportOverrides)
const reportId = computed(() => props.reportId)
const hideFilter = computed(() => props.hideFilter)

// Local filter state synced via events
const filters = ref<FilterGroup[]>([])
const filterInstanceId = `widget-${props.widget?.id || 'unknown'}-${Date.now()}`

// Listen for filter changes
function handleFilterUpdate(ev: Event) {
  const detail = (ev as CustomEvent).detail
  if (!detail || detail.source === filterInstanceId) return
  if (reportId.value && detail.reportId !== reportId.value) return
  filters.value = JSON.parse(JSON.stringify(detail.filters || []))
}

onMounted(() => {
  window.addEventListener('filter:updated', handleFilterUpdate)
})

onUnmounted(() => {
  window.removeEventListener('filter:updated', handleFilterUpdate)
})

// Check if we have data to filter
const hasData = computed(() => {
  const rows = widget.value?.last_step?.data?.rows
  return Array.isArray(rows) && rows.length > 0
})

// Check if this visualization has active filters
const hasActiveFilters = computed(() => {
  if (!reportId.value) return false
  const vizId = widget.value?.id || ''
  return filters.value.some(group =>
    group.conditions.some(c => c.column.startsWith(`${vizId}:`))
  )
})

// Apply filters to widget data
const filteredWidget = computed(() => {
  if (!reportId.value || !hasData.value || !filters.value.length) return widget.value
  
  const rows = widget.value.last_step?.data?.rows || []
  const vizId = widget.value?.id || ''
  const filteredRows = rows.filter((row: any) => evaluateFilters(row, filters.value, vizId))
  
  // If no filtering happened, return original widget
  if (filteredRows.length === rows.length) return widget.value
  
  // Return widget with filtered data
  return {
    ...widget.value,
    last_step: {
      ...widget.value.last_step,
      data: {
        ...widget.value.last_step.data,
        rows: filteredRows
      }
    }
  }
})

const compCache = new Map<string, any>()
function getCompForType(type?: string | null) {
  const t = (type || '').toLowerCase()
  if (!t) return null as any
  if (compCache.has(t)) return compCache.get(t)
  const entry = resolveEntryByType(t)
  if (!entry) return null as any
  const comp = defineAsyncComponent(entry.load)
  compCache.set(t, comp)
  return comp
}

const resolvedComp = computed(() => {
  // Support v2 schema (view.view.type) and legacy (view.type, data_model.type)
  const viewObj = widget.value?.view
  const vType = viewObj?.view?.type || viewObj?.type
  const dmType = widget.value?.last_step?.data_model?.type
  return getCompForType(String(vType || dmType || ''))
})
const isTable = computed(() => {
  const viewObj = widget.value?.view
  const t = String((viewObj?.view?.type || viewObj?.type || widget.value?.last_step?.data_model?.type || '')).toLowerCase()
  return t === 'table'
})
const tableComp = TableAgGrid

function deepMerge(target: any, source: any) {
  const out: any = Array.isArray(target) ? [...target] : { ...target }
  if (!source || typeof source !== 'object') return out
  Object.keys(source).forEach((key) => {
    const sv: any = (source as any)[key]
    if (sv && typeof sv === 'object' && !Array.isArray(sv)) {
      out[key] = deepMerge(out[key] || {}, sv)
    } else {
      out[key] = sv
    }
  })
  return out
}

const resolvedView = computed(() => {
  const stepView = widget.value?.last_step?.view || null
  const vizView = widget.value?.view || null
  const layoutOverrides = widget.value?.layout_view_overrides || null
  if (!layoutOverrides && !vizView && !stepView) return null
  // Merge order: step.view -> viz.view -> layout overrides (each overrides previous)
  const mergedStepViz = deepMerge(stepView || {}, vizView || {})
  return deepMerge(mergedStepViz, layoutOverrides || {})
})

// Prefer explicit widget.view (already merged in DashboardComponent) when available
const finalView = computed(() => {
  return (widget.value?.view && Object.keys(widget.value.view || {}).length > 0)
    ? widget.value.view
    : (resolvedView.value || null)
})
</script>


