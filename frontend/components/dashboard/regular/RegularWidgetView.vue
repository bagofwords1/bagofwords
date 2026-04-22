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
import { computed, defineAsyncComponent, ref, onMounted, onUnmounted, watch } from 'vue'
import SpinnerComponent from '@/components/SpinnerComponent.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'
import TableAgGrid from '@/components/dashboard/table/TableAgGrid.vue'
import VisualizationFilter from '@/components/dashboard/VisualizationFilter.vue'
import { evaluateFilters, parseColumnKey, type FilterGroup } from '~/composables/useSharedFilters'

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

// Seed shared filters from view.defaultFilters once per widget id. This lets a
// granular-data visualization open in a filtered state without the user having
// to click through the filter UI.
const seededDefaultsFor = ref<Set<string>>(new Set())

function seedDefaultFiltersFromView() {
  const vizId = String(widget.value?.id || '')
  if (!vizId || seededDefaultsFor.value.has(vizId)) return

  const viewObj = widget.value?.view as any
  const viewInner = viewObj?.view || viewObj
  const defaults = Array.isArray(viewInner?.defaultFilters) ? viewInner.defaultFilters : []
  if (!defaults.length) {
    seededDefaultsFor.value.add(vizId)
    return
  }

  const alreadyHasConditions = filters.value.some(g =>
    g.conditions.some(c => parseColumnKey(c.column).vizId === vizId)
  )
  if (alreadyHasConditions) {
    seededDefaultsFor.value.add(vizId)
    return
  }

  const conditions = defaults
    .filter((d: any) => d && typeof d.column === 'string' && d.column.length > 0)
    .map((d: any, i: number) => ({
      id: `default-${vizId}-${i}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
      column: `${vizId}:${d.column}`,
      operator: String(d.operator || 'equals'),
      value: d.value
    }))
  if (!conditions.length) {
    seededDefaultsFor.value.add(vizId)
    return
  }

  const group: FilterGroup = {
    id: `default-group-${vizId}-${Date.now()}`,
    conditions
  }
  const next = [...filters.value, group]
  filters.value = next
  seededDefaultsFor.value.add(vizId)

  try {
    window.dispatchEvent(new CustomEvent('filter:updated', {
      detail: { reportId: reportId.value, filters: next, source: filterInstanceId }
    }))
  } catch {}
}

watch(
  () => {
    const viewObj = widget.value?.view as any
    const viewInner = viewObj?.view || viewObj
    const defaults = Array.isArray(viewInner?.defaultFilters) ? viewInner.defaultFilters : []
    return [widget.value?.id, JSON.stringify(defaults)]
  },
  () => { seedDefaultFiltersFromView() },
  { immediate: true }
)

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


