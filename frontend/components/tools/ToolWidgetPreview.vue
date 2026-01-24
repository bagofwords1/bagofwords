<template>
  <div class="widget-container">
    <!-- Widget header with title and toggle -->
    <div class="widget-header" @click="toggleCollapsed">
      <div class="flex items-center justify-between w-full">
        <div class="flex items-center">
          <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3.5 h-3.5 mr-1.5 text-gray-500" />
          <h3 class="widget-title">{{ widgetTitle }}</h3>
          <button
            v-if="queryId && canEditCode && !readonly"
            @click.stop="onEditClick"
            class="text-xs px-2 py-0.5 text-gray-400 rounded transition-colors flex items-center"
            title="Edit query code"
          >
            <Icon name="heroicons-pencil-square" class="w-3.5 h-3.5 mr-1" />
            Edit
          </button>
        </div>
        <div class="flex items-center gap-2">
          <div v-if="rowCount" class="text-[11px] text-gray-400">
            {{ activeFilterCount > 0 ? `${filteredRowCount}/` : '' }}{{ rowCount }} rows
          </div>

          <button 
            v-if="hasDataForDownload"
            @click.stop="downloadCSV"
            class="text-gray-400 hover:text-gray-600 transition-colors"
            title="Download CSV"
          >
            <Icon name="heroicons:arrow-down-tray" class="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>

    <!-- Collapsible content -->
    <Transition name="slide-fade">
      <div v-if="!isCollapsed" class="widget-content">
        <!-- Error / empty state when step has an error -->
        <template v-if="hasStepError">
          <div class="min-h-[80px] flex items-center text-xs text-gray-400">
            No data is available.
          </div>
        </template>
        <template v-else>
          <!-- Tab Navigation -->
          <div v-if="showTabs" class="flex border-b border-gray-100 mb-2">
            <button 
              v-if="showVisual"
              @click="activeTab = 'chart'"
              :class="[
                'px-3 py-1.5 text-xs font-medium border-b-2 transition-colors',
                activeTab === 'chart' 
                  ? 'border-blue-500 text-blue-600' 
                  : 'border-transparent text-gray-400 hover:text-gray-600'
              ]"
            >
              Chart
            </button>
            <button 
              v-if="hasData"
              @click="activeTab = 'table'"
              :class="[
                'px-3 py-1.5 text-xs font-medium border-b-2 transition-colors',
                activeTab === 'table' 
                  ? 'border-blue-500 text-blue-600' 
                  : 'border-transparent text-gray-400 hover:text-gray-600'
              ]"
            >
              Data
            </button>
            <button 
              v-if="hasCode"
              @click="activeTab = 'code'"
              :class="[
                'px-3 py-1.5 text-xs font-medium border-b-2 transition-colors',
                activeTab === 'code' 
                  ? 'border-blue-500 text-blue-600' 
                  : 'border-transparent text-gray-400 hover:text-gray-600'
              ]"
            >
              Code
            </button>
          </div>

          <!-- Filter Row (above chart) - uses shared filter system -->
          <div v-if="hasData && visualizationId && reportId" class="flex justify-end mb-2">
            <VisualizationFilter
              :report-id="reportId"
              :visualization-id="visualizationId"
              :rows="effectiveStep?.data?.rows || []"
              :columns="effectiveStep?.data?.columns"
            />
          </div>

          <!-- Tab Content -->
          <div class="tab-content">
            <!-- Chart Content -->
            <Transition name="fade" mode="out-in">
              <div v-if="(showTabs && activeTab === 'chart') || (!showTabs && showVisual)">
                <div v-if="resolvedCompEl" :class="chartHeightClass" :style="chartHeightStyle">
                  <component
                    :is="resolvedCompEl"
                    :widget="effectiveWidget"
                    :data="filteredData"
                    :data_model="effectiveStep?.data_model"
                    :step="filteredStep"
                    :view="normalizedView"
                    :reportThemeName="reportThemeName"
                    :reportOverrides="reportOverrides"
                  />
                </div>
                <div v-else-if="chartVisualTypes.has(effectiveStep?.data_model?.type)" class="h-[340px]">
                  <RenderVisual :widget="effectiveWidget" :data="filteredData" :data_model="effectiveStep?.data_model" :view="normalizedView" />
                </div>
              </div>
            </Transition>

            <!-- Table Content -->
            <Transition name="fade" mode="out-in">
              <div
                v-if="(showTabs && activeTab === 'table') || (!showTabs && isTableType)"
                :class="tableHeightClass"
              >
                <RenderTable :widget="widget" :step="{ ...(filteredStep || {}), data_model: { ...(effectiveStep?.data_model || {}), type: 'table' } } as any" />
              </div>
            </Transition>

            <!-- Code Content -->
            <Transition name="fade" mode="out-in">
              <div
                v-if="(showTabs && activeTab === 'code') || (!showTabs && hasCode && !showVisual && !hasData)"
              >
                <div class="relative">
                  <!-- Edit button top right -->
                  <button
                    v-if="queryId && canEditCode && !readonly"
                    @click="onEditClick"
                    class="absolute top-2 right-2 z-10 text-xs px-2 py-1 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded transition-colors flex items-center"
                    title="Edit code"
                  >
                    <Icon name="heroicons-pencil-square" class="w-3 h-3 mr-1" />
                    Edit
                  </button>
                  <div class="max-h-[300px] overflow-auto">
                    <pre class="bg-gray-50 rounded px-4 py-3 pr-20 font-mono text-xs text-gray-800 whitespace-pre-wrap">{{ effectiveStep?.code }}</pre>
                  </div>
                </div>
                
                <!-- Execution details -->
                <div v-if="executionDuration || rowCount" class="mt-2 flex items-center gap-3 text-[11px] text-gray-400">
                  <span v-if="executionDuration">
                    <Icon name="heroicons-clock" class="w-3 h-3 inline-block mr-1" />
                    {{ executionDuration }}
                  </span>
                  <span v-if="rowCount">
                    <Icon name="heroicons-table-cells" class="w-3 h-3 inline-block mr-1" />
                    {{ rowCount }} rows
                  </span>
                </div>
                
                <!-- Attempts section -->
                <div v-if="attempts.length > 0" class="mt-3 border-t border-gray-100 pt-3">
                  <div 
                    class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
                    @click="attemptsExpanded = !attemptsExpanded"
                  >
                    <Icon :name="attemptsExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1.5" />
                    <span>Attempts ({{ attempts.length }})</span>
                  </div>
                  <Transition name="fade">
                    <div v-if="attemptsExpanded" class="mt-2 ml-4">
                      <ul class="text-xs text-gray-600 space-y-1.5">
                        <li v-for="(att, idx) in attempts" :key="idx" class="flex items-start">
                          <span class="text-gray-400 mr-2 flex-shrink-0">{{ idx + 1 }}.</span>
                          <span class="text-red-500">{{ att }}</span>
                        </li>
                      </ul>
                    </div>
                  </Transition>
                </div>
              </div>
            </Transition>
          </div>
        </template>

        <!-- Bottom Action Buttons (hidden in readonly mode) -->
        <div v-if="!readonly" class="mt-2 pt-2 border-t border-gray-100 flex items-center">
          <div class="flex items-center space-x-2">
            <button
              v-if="!effectiveStep?.created_entity_id"
              class="text-xs px-2 py-0.5 rounded transition-colors flex items-center hover:bg-gray-50"
              @click.stop="openEntityModal = true"
            >
              <Icon name="heroicons-bookmark" class="w-3.5 h-3.5 text-blue-500 mr-1" />
              Save Query
            </button>
            <span v-else class="text-xs flex items-center">
              <Icon name="heroicons-check-badge" class="w-3.5 h-3.5 mr-1 text-green-500" />
              Saved Query
            </span>
          </div>
        </div>

      </div>
    </Transition>
    <!-- Save as Entity Modal -->
    <EntityCreateModal
      :visible="openEntityModal"
      :initialTitle="widgetTitle"
      :initialCode="effectiveStep?.code || ''"
      :initialView="visualization?.view || (effectiveStep?.view || null)"
      :initialData="effectiveStep?.data || null"
      :dataModel="effectiveStep?.data_model || null"
      :stepId="effectiveStep?.id || null"
      :initialDataSourceIds="reportDataSources"
      @close="openEntityModal = false"
      @saved="handleEntitySaved"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, defineAsyncComponent, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useMyFetch } from '~/composables/useMyFetch'
import { useOrgSettings } from '~/composables/useOrgSettings'
import RenderVisual from '../RenderVisual.vue'
import RenderTable from '../RenderTable.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'
import EntityCreateModal from '../entity/EntityCreateModal.vue'
import VisualizationFilter from '@/components/dashboard/VisualizationFilter.vue'
import { 
  parseColumnKey, 
  evaluateFilters as sharedEvaluateFilters,
  type FilterGroup 
} from '~/composables/useSharedFilters'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  created_widget_id?: string
  created_step_id?: string
  created_widget?: any
  created_step?: any
  created_visualizations?: Array<{ id: string; title?: string; status?: string; report_id?: string; query_id?: string; view?: Record<string, any> }>
}

const props = defineProps<{
  toolExecution: ToolExecution
  readonly?: boolean
  initialCollapsed?: boolean
}>()
const emit = defineEmits(['toggleSplitScreen', 'editQuery'])

const { canEditCode } = useOrgSettings()

// Reactive state for collapsible behavior
const isCollapsed = ref(props.initialCollapsed ?? false)
const layoutBlocks = ref<any[]>([])
const route = useRoute()
const reportId = computed(() => String(route.params.id || ''))
const reportThemeName = ref<string | null>(null)
const reportOverrides = ref<Record<string, any> | null>(null)
const reportDataSources = ref<string[]>([])
const openEntityModal = ref(false)
const attemptsExpanded = ref(false)

// Tab state - default to chart if available, otherwise table, then code
const activeTab = ref<'chart' | 'table' | 'code'>('chart')

const widget = computed(() => props.toolExecution?.created_widget || null)
const step = computed(() => {
  // First try created_step
  if (props.toolExecution?.created_step) {
    return props.toolExecution.created_step
  }
  // Fallback: build synthetic step from result_json (for public/readonly views)
  const rj = props.toolExecution?.result_json as any
  if (rj?.data?.rows || rj?.data?.columns) {
    return {
      id: props.toolExecution?.created_step_id || `step-${props.toolExecution?.id || 'preview'}`,
      title: rj?.title || rj?.widget_title || 'Results',
      code: rj?.code || '',
      data: rj?.data || {},
      data_model: rj?.data_model || { type: 'table' },
      view: rj?.view || null,
      status: 'success',
    }
  }
  return null
})
const stepOverride = ref<any | null>(null)
const effectiveStep = computed(() => stepOverride.value || step.value)
const hydratedVisualization = ref<any | null>(null)

const visualization = computed(() => {
  if (hydratedVisualization.value) return hydratedVisualization.value
  const list = (props.toolExecution as any)?.created_visualizations
  if (Array.isArray(list) && list.length) return list[0]
  // Fallback: build synthetic visualization from result_json (for public/readonly views)
  // Mark as synthetic so we don't allow adding to dashboard with wrong ID
  const rj = props.toolExecution?.result_json as any
  if (rj?.view || rj?.data_model) {
    return {
      id: `viz-${props.toolExecution?.id || 'preview'}`,
      title: rj?.title || rj?.widget_title || 'Results',
      view: rj?.view || { type: rj?.data_model?.type || 'table' },
      status: 'success',
      _isSynthetic: true,  // Flag to prevent adding to dashboard
    }
  }
  return null
})

// ============ SHARED FILTER LOGIC ============
// Filters are now managed via shared events (filter:updated) for dashboard synchronization
const sharedFilters = ref<FilterGroup[]>([])
const filterInstanceId = `toolpreview-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

// Visualization ID for filter targeting
const visualizationId = computed(() => {
  const v = visualization.value as any
  return v?.id || null
})

// Listen for shared filter updates
function handleSharedFilterUpdate(ev: Event) {
  const detail = (ev as CustomEvent).detail
  if (!detail || detail.source === filterInstanceId) return
  if (reportId.value && detail.reportId !== reportId.value) return
  sharedFilters.value = JSON.parse(JSON.stringify(detail.filters || []))
}

// Count active filters for this visualization
const activeFilterCount = computed(() => {
  if (!visualizationId.value) return 0
  let count = 0
  for (const group of sharedFilters.value) {
    for (const cond of group.conditions) {
      const { vizId } = parseColumnKey(cond.column)
      if (vizId === visualizationId.value) count++
    }
  }
  return count
})

// Apply shared filters to get filtered rows
const filteredRows = computed(() => {
  const rows = effectiveStep.value?.data?.rows
  if (!Array.isArray(rows)) return []
  if (sharedFilters.value.length === 0 || !visualizationId.value) return rows
  
  return rows.filter((row: any) => 
    sharedEvaluateFilters(row, sharedFilters.value, visualizationId.value)
  )
})

const filteredRowCount = computed(() => filteredRows.value.length)

// Filtered data object for components
const filteredData = computed(() => {
  if (!effectiveStep.value?.data) return null
  return {
    ...effectiveStep.value.data,
    rows: filteredRows.value
  }
})

// Filtered step for table component
const filteredStep = computed(() => {
  if (!effectiveStep.value) return null
  return {
    ...effectiveStep.value,
    data: filteredData.value
  }
})
// ============ END SHARED FILTER LOGIC ============

// Normalize the view to ensure it's in the v2 format { view: {...}, version: 'v2' }
const normalizedView = computed(() => {
  const v = visualization.value?.view || (step.value as any)?.view
  if (!v) return null
  // Already in v2 format (has .view.type)
  if (v.view?.type) return v
  // Flat format - wrap it
  if (v.type) return { view: v, version: 'v2' }
  return v
})

// Provide a stable widget object for children even if upstream is null
const effectiveWidget = computed(() => {
  const v = visualization.value as any
  const w = widget.value as any
  if (w && w.id) return w
  return { id: v?.id || (props.toolExecution as any)?.created_step_id || 'preview', title: v?.title || widgetTitle.value } as any
})

// Derive query id from available sources
const queryId = computed(() => {
  const v = visualization.value as any
  const s = effectiveStep.value as any
  return v?.query_id || s?.query_id || (props.toolExecution as any)?.result_json?.query_id || null
})

async function hydrateVisualizationIfNeeded() {
  try {
    const v = visualization.value as any
    // Always hydrate if we only have a synthetic visualization (no real viz ID)
    if (v?.id && v?.status && !v?._isSynthetic) return
    if (!queryId.value) return
    const { data, error } = await useMyFetch(`/api/queries/${queryId.value}`, { method: 'GET' })
    if (error.value) return
    const q = data.value as any
    const vList = (q && Array.isArray(q.visualizations)) ? q.visualizations : []
    const ok = vList.find((it: any) => it?.status === 'success') || vList[0]
    if (ok) hydratedVisualization.value = ok
  } catch (_) {
    // noop
  }
}

// Widget title from various sources
const widgetTitle = computed(() => {
  return widget.value?.title || 
         effectiveStep.value?.title || 
         props.toolExecution?.result_json?.widget_title ||
         'Results'
})

// Row count for display
const rowCount = computed(() => {
  const rows = effectiveStep.value?.data?.rows
  if (Array.isArray(rows)) {
    return `${rows.length.toLocaleString()}`
  }
  return null
})

// Execution duration for display
const executionDuration = computed(() => {
  const ms = (props.toolExecution as any)?.duration_ms
  if (!ms || ms < 100) return null
  if (ms < 1000) return `${ms}ms`
  const seconds = (ms / 1000).toFixed(1)
  return `${seconds}s`
})

const chartVisualTypes = new Set<string>([
  'pie_chart',
  'line_chart',
  'bar_chart',
  'area_chart',
  'heatmap',
  'scatter_plot',
  'map',
  'candlestick',
  'treemap',
  'radar_chart'
])

const showVisual = computed(() => {
  const viewObj = visualization.value?.view as any
  const vType = viewObj?.view?.type || viewObj?.type
  const t = vType || effectiveStep.value?.data_model?.type
  if (!t) return false
  const entry = resolveEntryByType(String(t).toLowerCase())
  if (entry) {
    // treat table as data-only; everything else is a visual
    return entry.componentKey !== 'table.aggrid'
  }
  return chartVisualTypes.has(String(t)) || String(t) === 'count' || String(t) === 'metric_card'
})

// Dashboard registry-driven dynamic component
const compCache = new Map<string, any>()
function getCompForType(type?: string | null) {
  const t = (type || '').toLowerCase()
  if (!t) return null
  if (compCache.has(t)) return compCache.get(t)
  const entry = resolveEntryByType(t)
  if (!entry) return null
  const comp = defineAsyncComponent(entry.load)
  compCache.set(t, comp)
  return comp
}
// Prefer the visualization.view.type if available; fall back to data_model.type
// Support both v2 schema (view.view.type) and legacy (view.type)
const resolvedCompEl = computed(() => {
  const viewObj = visualization.value?.view as any
  const vType = viewObj?.view?.type || viewObj?.type
  const dmType = effectiveStep.value?.data_model?.type
  return getCompForType(String(vType || dmType || ''))
})

// Check if current visualization is a metric card type
const isMetricCardType = computed(() => {
  const viewObj = visualization.value?.view as any
  const t = String((viewObj?.view?.type || viewObj?.type || effectiveStep.value?.data_model?.type || '')).toLowerCase()
  return t === 'count' || t === 'metric_card'
})

// Adjust height for compact metric cards - dynamically based on content
const chartHeightClass = computed(() => {
  return isMetricCardType.value ? 'flex items-start' : 'h-[340px]'
})

// Dynamic style for metric card height
const chartHeightStyle = computed(() => {
  if (!isMetricCardType.value) return {}
  
  const viewObj = visualization.value?.view as any
  const viewConfig = viewObj?.view || viewObj || {}
  
  // Check if sparkline is enabled
  const hasSparkline = viewConfig?.sparkline?.enabled === true
  const sparklineHeight = viewConfig?.sparkline?.height || 64
  
  // Check if comparison/trend is present
  const hasComparison = !!viewConfig?.comparison
  
  // Base height for title + value (with padding)
  let height = 120
  // Add space for comparison row if present
  if (hasComparison) height += 28
  // Add sparkline height if enabled (no extra padding - chart is edge to edge)
  if (hasSparkline) height += sparklineHeight
  
  // Minimum height to ensure good appearance
  height = Math.max(height, 160)
  
  return { height: `${height}px` }
})

// Determine if table/data is present
const hasData = computed(() => {
  const rows = effectiveStep.value?.data?.rows
  if (Array.isArray(rows)) return rows.length >= 0
  // If structure differs, still attempt to show table; RenderTable guards internal nulls
  return !!effectiveStep.value
})

// Check if code is available
const hasCode = computed(() => !!effectiveStep.value?.code)

// Get attempts/errors from tool execution
const attempts = computed(() => {
  const errs = (props.toolExecution?.result_json as any)?.errors || []
  return errs.map((pair: any) => {
    const msg = Array.isArray(pair) ? pair[1] : (pair?.message || String(pair))
    const firstLine = (msg || '').split('\n')[0]
    return firstLine
  })
})

// Show tabs when we have multiple content types available
const showTabs = computed(() => {
  const contentTypes = [showVisual.value, hasData.value, hasCode.value].filter(Boolean).length
  return contentTypes > 1
})

// Error / table-specific helpers
const hasStepError = computed(() => {
  const s: any = effectiveStep.value as any
  if (!s) return false
  if (s.error) return true
  const status = String(s.status || '').toLowerCase()
  return status === 'error' || status === 'fail' || status === 'failed'
})

const tableHasRows = computed(() => {
  const rows = effectiveStep.value?.data?.rows
  return Array.isArray(rows) && rows.length > 0
})

const tableHeightClass = computed(() => (tableHasRows.value ? 'h-[400px]' : 'min-h-[80px]'))

// Check if current type is table
const isTableType = computed(() => {
  const viewObj = visualization.value?.view as any
  const t = String((viewObj?.view?.type || viewObj?.type || effectiveStep.value?.data_model?.type || '')).toLowerCase()
  return t === 'table'
})

// Watch for data changes to update active tab
watch([showVisual, hasData, hasCode], () => {
  if (showVisual.value) {
    activeTab.value = 'chart'
  } else if (hasData.value) {
    activeTab.value = 'table'
  } else if (hasCode.value) {
    activeTab.value = 'code'
  }
}, { immediate: true })

function toggleCollapsed() {
  isCollapsed.value = !isCollapsed.value
}

// CSV download functionality
const hasDataForDownload = computed(() => {
  const rows = effectiveStep.value?.data?.rows
  return Array.isArray(rows) && rows.length > 0
})

function downloadCSV() {
  const rows = effectiveStep.value?.data?.rows
  const columns = effectiveStep.value?.data?.columns
  
  if (!Array.isArray(rows) || !Array.isArray(columns) || rows.length === 0) {
    return
  }

  // Create CSV content
  const headers = columns.map(col => col.field || col.headerName || col.colId || '').join(',')
  const csvRows = rows.map(row => 
    columns.map(col => {
      const field = col.field || col.colId
      const value = row[field] || ''
      // Escape quotes and wrap in quotes if contains comma or quote
      const stringValue = String(value)
      if (stringValue.includes(',') || stringValue.includes('"') || stringValue.includes('\n')) {
        return `"${stringValue.replace(/\"/g, '""')}"`
      }
      return stringValue
    }).join(',')
  )
  
  const csvContent = [headers, ...csvRows].join('\n')
  
  // Create and trigger download
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' })
  const link = document.createElement('a')
  const url = URL.createObjectURL(blob)
  link.setAttribute('href', url)
  link.setAttribute('download', `${widgetTitle.value || 'data'}.csv`)
  link.style.visibility = 'hidden'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

// Helper for external broadcasts
function broadcastDefaultStep(step: any) {
  try {
    if (step?.query_id) {
      window.dispatchEvent(new CustomEvent('query:default_step_changed', { detail: { query_id: step.query_id, step } }))
    }
  } catch {}
}

// Keep membership state in sync when dashboard layout changes elsewhere
onMounted(() => {
  // Listen for shared filter updates from VisualizationFilter and FilterBuilder
  window.addEventListener('filter:updated', handleSharedFilterUpdate as any)
  
  function handleLayoutChanged(ev: CustomEvent) {
    try {
      const detail: any = (ev as any)?.detail || {}
      // Trigger recomputation by refreshing membership list
      refreshMembership()
    } catch {}
  }
  window.addEventListener('dashboard:layout_changed', handleLayoutChanged as any)
  function handleVizUpdated(ev: CustomEvent) {
    try {
      const detail: any = (ev as any)?.detail || {}
      const id: string | undefined = detail?.id
      const updated: any = detail?.visualization
      const current = visualization.value as any
      if (!id || !updated || !current?.id) return
      if (String(current.id) !== String(id)) return
      // Update local hydrated viz so preview re-renders with latest view/title
      hydratedVisualization.value = JSON.parse(JSON.stringify({ ...(current || {}), ...(updated || {}) }))
    } catch {}
  }
  window.addEventListener('visualization:updated', handleVizUpdated as any)
  // Store removers on instance for cleanup
  ;(window as any).__tw_preview_handlers__ = { handleLayoutChanged, handleVizUpdated }
  // Load report theme and data sources so preview uses same styling as dashboard
  ;(async () => {
    try {
      if (!reportId.value) return
      const { data, error } = await useMyFetch(`/api/reports/${reportId.value}`, { method: 'GET' })
      if (error.value) return
      const r: any = data.value
      reportThemeName.value = r?.report_theme_name || r?.theme_name || null
      reportOverrides.value = r?.theme_overrides || null
      // Extract data source IDs from the report
      if (r?.data_sources && Array.isArray(r.data_sources)) {
        reportDataSources.value = r.data_sources.map((ds: any) => String(ds.id))
      }
    } catch {}
  })()
  // Live theme updates from dashboard
  function handleThemeChanged(ev: CustomEvent) {
    try {
      const detail: any = (ev as any)?.detail || {}
      if (!detail) return
      if (String(detail.report_id || '') !== String(reportId.value || '')) return
      reportThemeName.value = detail.themeName || null
      reportOverrides.value = detail.overrides ? JSON.parse(JSON.stringify(detail.overrides)) : null
    } catch {}
  }
  window.addEventListener('dashboard:theme_changed', handleThemeChanged as any)
  ;(window as any).__tw_preview_handlers__.handleThemeChanged = handleThemeChanged
  // On initial mount, if we can resolve a query id, fetch the latest default step
  ;(async () => {
    try {
      const qid = queryId.value
      if (qid) {
        const { data, error } = await useMyFetch(`/api/queries/${qid}/default_step`, { method: 'GET' })
        if (!error.value) {
          const fetched = ((data.value as any) || {}).step || null
          if (fetched) stepOverride.value = JSON.parse(JSON.stringify(fetched))
        }
      }
    } catch {}
  })()
  // Update local step when the editor broadcasts a new default step for this query
  function handleDefaultStepChanged(ev: CustomEvent) {
    try {
      const detail: any = (ev as any)?.detail || {}
      if (!detail?.query_id) return
      if (String(detail.query_id) !== String(queryId.value || '')) return
      // Always fetch the latest default step from backend to avoid stale payloads
      ;(async () => {
        try {
          const { data, error } = await useMyFetch(`/api/queries/${detail.query_id}/default_step`, { method: 'GET' })
          if (!error.value) {
            const fetched = ((data.value as any) || {}).step || null
            if (fetched) {
              stepOverride.value = JSON.parse(JSON.stringify(fetched))
            } else if (detail.step) {
              stepOverride.value = JSON.parse(JSON.stringify(detail.step))
            }
          } else if (detail.step) {
            stepOverride.value = JSON.parse(JSON.stringify(detail.step))
          }
        } catch {
          if (detail.step) {
            stepOverride.value = JSON.parse(JSON.stringify(detail.step))
          }
        }
      })()
    } catch {}
  }
  window.addEventListener('query:default_step_changed', handleDefaultStepChanged as any)
  ;(window as any).__tw_preview_handlers__.handleDefaultStepChanged = handleDefaultStepChanged
  // Allow editor to explicitly rebind this preview to a specific query id
  function handleToolPreviewRebind(ev: CustomEvent) {
    try {
      const detail: any = (ev as any)?.detail || {}
      const teid: string | undefined = detail?.tool_execution_id
      const qid: string | undefined = detail?.query_id
      if (!teid || String(teid) !== String((props.toolExecution as any)?.id || (props.toolExecution as any)?.created_step_id || '')) return
      if (!qid) return
      // Update visualization/query binding and fetch the latest default step immediately
      hydratedVisualization.value = null
      ;(async () => {
        try {
          const { data, error } = await useMyFetch(`/api/queries/${qid}/default_step`, { method: 'GET' })
          if (!error.value) {
            const fetched = ((data.value as any) || {}).step || null
            if (fetched) {
              stepOverride.value = JSON.parse(JSON.stringify(fetched))
            }
          }
        } catch {}
      })()
    } catch {}
  }
  window.addEventListener('tool_preview:rebind', handleToolPreviewRebind as any)
  ;(window as any).__tw_preview_handlers__.handleToolPreviewRebind = handleToolPreviewRebind
})

onUnmounted(() => {
  // Remove shared filter listener
  try { window.removeEventListener('filter:updated', handleSharedFilterUpdate as any) } catch {}
  
  const handlers: any = (window as any).__tw_preview_handlers__
  if (handlers) {
    try { window.removeEventListener('dashboard:layout_changed', handlers.handleLayoutChanged as any) } catch {}
    try { window.removeEventListener('visualization:updated', handlers.handleVizUpdated as any) } catch {}
    try { window.removeEventListener('dashboard:theme_changed', handlers.handleThemeChanged as any) } catch {}
    try { window.removeEventListener('query:default_step_changed', handlers.handleDefaultStepChanged as any) } catch {}
    try { window.removeEventListener('tool_preview:rebind', handlers.handleToolPreviewRebind as any) } catch {}
    ;(window as any).__tw_preview_handlers__ = undefined
  }
})

async function refreshMembership() {
  try {
    if (!reportId.value) return
    const { data, error } = await useMyFetch(`/api/reports/${reportId.value}/layouts?hydrate=true`, { method: 'GET' })
    if (error.value) throw error.value
    const layouts = Array.isArray(data.value) ? data.value : []
    const active = layouts.find((l: any) => l.is_active)
    layoutBlocks.value = active?.blocks || []
  } catch (e) {
    // noop
  }
}

function onEditClick() {
  if (!queryId.value) return
  
  // Emit event with query information for opening the editor
  emit('editQuery', {
    queryId: queryId.value,
    stepId: step.value?.id || null,
    initialCode: step.value?.code || '',
    title: widgetTitle.value
  })
}

async function handleEntitySaved() {
  openEntityModal.value = false
  
  // Refresh the step to get the updated created_entity_id
  try {
    const qid = queryId.value
    if (qid) {
      const { data, error } = await useMyFetch(`/api/queries/${qid}/default_step`, { method: 'GET' })
      if (!error.value) {
        const fetched = ((data.value as any) || {}).step || null
        if (fetched) {
          stepOverride.value = JSON.parse(JSON.stringify(fetched))
        }
      }
    }
  } catch (e) {
    console.error('Error refreshing step after entity save:', e)
  }
}

onMounted(() => {
  refreshMembership()
  hydrateVisualizationIfNeeded()
})
</script>

<style scoped>
.widget-container {
  @apply mt-2 mb-2 border border-gray-100 rounded-lg bg-white shadow-sm;
}

.widget-header {
  @apply px-3 py-2 cursor-pointer hover:bg-gray-50 border-b border-gray-100 transition-colors duration-150;
}

.widget-title {
  @apply text-xs font-medium text-gray-700 select-none;
}

.widget-content {
  @apply p-3;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.slide-fade-enter-active {
  transition: all 0.2s ease-out;
}

.slide-fade-leave-active {
  transition: all 0.2s ease-in;
}

.slide-fade-enter-from,
.slide-fade-leave-to {
  transform: translateY(-10px);
  opacity: 0;
}
</style>


