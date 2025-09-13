<template>
  <div class="widget-container">
    <!-- Widget header with title and toggle -->
    <div class="widget-header" @click="toggleCollapsed">
      <div class="flex items-center justify-between w-full">
        <div class="flex items-center">
          <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3.5 h-3.5 mr-1.5 text-gray-500" />
          <h3 class="widget-title">{{ widgetTitle }}</h3>
        </div>
        <div class="flex items-center gap-2">
          <div v-if="rowCount" class="text-[11px] text-gray-400">
            {{ rowCount }} rows
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
        </div>

        <!-- Tab Content -->
        <div class="tab-content">
          <!-- Chart Content -->
          <Transition name="fade" mode="out-in">
            <div v-if="(showTabs && activeTab === 'chart') || (!showTabs && showVisual)" class="bg-gray-50 rounded-sm p-2">
              <!-- Prefer explicit count renderer to avoid async component timing -->
              <div v-if="effectiveStep?.data_model?.type === 'count'" class="h-[340px] flex items-start">
                <RenderCount :show_title="true" :widget="effectiveWidget" :data="effectiveStep?.data" :data_model="effectiveStep?.data_model" />
              </div>
              <div v-else-if="resolvedCompEl" class="h-[340px]">
                <component
                  :is="resolvedCompEl"
                  :widget="effectiveWidget"
                  :data="effectiveStep?.data"
                  :data_model="effectiveStep?.data_model"
                  :step="effectiveStep"
                  :view="visualization?.view || step?.view"
                  :reportThemeName="reportThemeName"
                  :reportOverrides="reportOverrides"
                />
              </div>
              <div v-else-if="chartVisualTypes.has(effectiveStep?.data_model?.type)" class="h-[340px]">
                <RenderVisual :widget="effectiveWidget" :data="effectiveStep?.data" :data_model="effectiveStep?.data_model" />
              </div>
            </div>
          </Transition>

          <!-- Table Content -->
          <Transition name="fade" mode="out-in">
            <div v-if="(showTabs && activeTab === 'table') || (!showTabs && (String((visualization?.view as any)?.type || effectiveStep?.data_model?.type || '').toLowerCase() === 'table'))" class="h-[400px]">
              <RenderTable :widget="widget" :step="{ ...(effectiveStep || {}), data_model: { ...(effectiveStep?.data_model || {}), type: 'table' } } as any" />
            </div>
          </Transition>
        </div>

        <!-- Bottom Action Buttons -->
        <div class="mt-2 pt-2 border-t border-gray-100 flex justify-between items-center">
          <div class="flex items-center space-x-2">
            <button
              v-if="!isPublished"
              :disabled="!canAdd || isAdding"
              @click="onAddClick"
              class="text-xs px-2 py-0.5 rounded transition-colors"
              :class="[
                canAdd && !isAdding ? 'hover:bg-gray-50' : 'text-gray-400 cursor-not-allowed'
              ]"
            >
              <Icon v-if="isAdding" name="heroicons-arrow-path" class="w-3.5 h-3.5 inline-block mr-1 animate-spin" />
              <span v-if="!canAdd">Generating…</span>
              <span v-else class="flex items-center">
                  <Icon name="heroicons-chart-pie" class="w-3.5 h-3.5 text-blue-500 inline-block mr-1" />
                  Add to dashboard</span>
            </button>
            <span v-else class="text-xs flex items-center">
              <Icon name="heroicons-check" class="w-3.5 h-3.5 mr-1 text-green-500" />
              Added to dashboard</span>
          </div>
          
          <button
            v-if="queryId"
            @click="onEditClick"
            class="text-xs px-2 py-0.5 rounded transition-colors hover:bg-gray-50 text-gray-600 hover:text-gray-800 flex items-center"
            title="Edit query code"
          >
            <Icon name="heroicons-pencil-square" class="w-3.5 h-3.5 mr-1" />
            Edit
          </button>
        </div>

      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, defineAsyncComponent, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useMyFetch } from '~/composables/useMyFetch'
import RenderVisual from '../RenderVisual.vue'
import RenderCount from '../RenderCount.vue'
import RenderTable from '../RenderTable.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'

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

const props = defineProps<{ toolExecution: ToolExecution }>()
const emit = defineEmits(['addWidget', 'toggleSplitScreen', 'editQuery'])

// Reactive state for collapsible behavior
const isCollapsed = ref(false) // Start expanded
const isAdding = ref(false)
const layoutBlocks = ref<any[]>([])
const route = useRoute()
const reportId = computed(() => String(route.params.id || ''))
const reportThemeName = ref<string | null>(null)
const reportOverrides = ref<Record<string, any> | null>(null)

// Tab state - default to chart if available, otherwise table
const activeTab = ref<'chart' | 'table'>('chart')

const widget = computed(() => props.toolExecution?.created_widget || null)
const step = computed(() => props.toolExecution?.created_step || null)
const stepOverride = ref<any | null>(null)
const effectiveStep = computed(() => stepOverride.value || step.value)
const hydratedVisualization = ref<any | null>(null)
const visualization = computed(() => {
  if (hydratedVisualization.value) return hydratedVisualization.value
  const list = (props.toolExecution as any)?.created_visualizations
  if (Array.isArray(list) && list.length) return list[0]
  return null
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
    if (v?.id && v?.status) return
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
  const vType = (visualization.value?.view as any)?.type
  const t = vType || effectiveStep.value?.data_model?.type
  if (!t) return false
  const entry = resolveEntryByType(String(t).toLowerCase())
  if (entry) {
    // treat table as data-only; everything else is a visual
    return entry.componentKey !== 'table.aggrid'
  }
  return chartVisualTypes.has(String(t)) || String(t) === 'count'
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
const resolvedCompEl = computed(() => {
  const vType = (visualization.value?.view as any)?.type
  const dmType = effectiveStep.value?.data_model?.type
  return getCompForType(String(vType || dmType || ''))
})

// Determine if table/data is present
const hasData = computed(() => {
  const rows = effectiveStep.value?.data?.rows
  if (Array.isArray(rows)) return rows.length >= 0
  // If structure differs, still attempt to show table; RenderTable guards internal nulls
  return !!effectiveStep.value
})

// Show tabs only when both chart and table are available
const showTabs = computed(() => showVisual.value && hasData.value)

// Watch for data changes to update active tab
watch([showVisual, hasData], () => {
  if (showVisual.value) {
    activeTab.value = 'chart'
  } else if (hasData.value) {
    activeTab.value = 'table'
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

// Add-to-dashboard gating and action
const isPublished = computed(() => {
  const vizId = visualization.value?.id
  if (!vizId) return false
  return layoutBlocks.value.some(b => b?.type === 'visualization' && b?.visualization_id === vizId)
})

watch(layoutBlocks, () => {
  // ensure computed re-evaluates when layout membership changes
}, { deep: true })
const canAdd = computed(() => {
  const viz = visualization.value
  const st = effectiveStep.value
  // Consider step OK if explicit status is success OR if rows are present
  const rows = st?.data?.rows
  const stepOk = st?.status ? st.status === 'success' : Array.isArray(rows)
  // Consider viz OK if explicit status is success OR if it has an id
  const vizOk = viz?.status ? viz.status === 'success' : !!viz?.id
  return !!(viz?.id && stepOk && vizOk)
})

// Keep membership state in sync when dashboard layout changes elsewhere
onMounted(() => {
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
  // Load report theme so preview uses same styling as dashboard
  ;(async () => {
    try {
      if (!reportId.value) return
      const { data, error } = await useMyFetch(`/api/reports/${reportId.value}`, { method: 'GET' })
      if (error.value) return
      const r: any = data.value
      reportThemeName.value = r?.report_theme_name || r?.theme_name || null
      reportOverrides.value = r?.theme_overrides || null
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

async function onAddClick() {
  if (isAdding.value) return
  if (!visualization.value?.id) return
  isAdding.value = true
  try {
    // Best-effort: patch layout directly
    if (reportId.value) {
      try {
        const { error } = await useMyFetch(`/api/reports/${reportId.value}/layouts/active/blocks`, {
          method: 'PATCH',
          body: {
            blocks: [{ type: 'visualization', visualization_id: visualization.value.id, x: 0, y: 0, width: 12, height: 8 }]
          }
        })
        if (error.value) throw error.value
        // Optimistically mark as published so the button flips immediately
        // locallyPublished.value = true // This line was removed as per the new_code, as locallyPublished is not defined.
        // Also update local membership list so the state is consistent
        layoutBlocks.value = [...layoutBlocks.value, { type: 'visualization', visualization_id: visualization.value.id }]
        // Broadcast to dashboard pane to refresh membership immediately
        try {
          window.dispatchEvent(new CustomEvent('dashboard:layout_changed', { detail: { report_id: reportId.value, action: 'added', visualization_id: visualization.value.id } }))
        } catch {}
        // Ensure dashboard pane is open, but do not close if already open
        try {
          window.dispatchEvent(new CustomEvent('dashboard:ensure_open'))
        } catch {}
      } catch (e) {
        // fallback to parent handler if exists
        emit('addWidget', { visualization: visualization.value, step: step.value })
        try { window.dispatchEvent(new CustomEvent('dashboard:ensure_open')) } catch {}
      }
    } else {
      emit('addWidget', { visualization: visualization.value, step: step.value })
      try { window.dispatchEvent(new CustomEvent('dashboard:ensure_open')) } catch {}
    }
    // Best-effort: refresh membership shortly after parent patches layout
    setTimeout(refreshMembership, 600)
  } finally {
    // Let parent control final state; keep short throttle to avoid double clicks
    setTimeout(() => { isAdding.value = false }, 800)
  }
}

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


