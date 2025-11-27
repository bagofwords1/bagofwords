<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-6xl', height: 'sm:h-[90vh]' }" :prevent-close="false">
    <div class="h-full flex flex-col" @click.stop>
      <div class="px-4 py-3 flex items-center justify-between flex-shrink-0">
        <div class="text-sm font-medium text-gray-800">Edit query — {{ title }}</div>
        <div v-if="currentStepId" class="ml-4 text-[11px] text-gray-500">Query ID: {{ queryId }}</div>
        <button class="px-3 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50" @click.stop="open = false">Close</button>
      </div>
      <div class="flex-1 flex flex-col overflow-hidden min-h-0">
        <!-- Top tabs -->
        <nav class="px-4 flex items-center space-x-4 border-b flex-shrink-0">
          <button
            v-if="canEditCode"
            class="py-2 text-xs transition-colors border-b-2 -mb-px"
            :class="activeTab === 'code' ? 'text-blue-600 border-blue-600 font-medium' : 'text-gray-500 border-transparent hover:text-gray-700'"
            @click="activeTab = 'code'"
          >Query</button>
          <button
            class="py-2 text-xs transition-colors border-b-2 -mb-px"
            :class="activeTab === 'visuals' ? 'text-blue-600 border-blue-600 font-medium' : 'text-gray-500 border-transparent hover:text-gray-700'"
            @click="activeTab = 'visuals'"
          >Visuals</button>
        </nav>

        <!-- Content -->
        <section class="flex-1 flex flex-col overflow-hidden min-h-0">
          <div v-if="canEditCode && activeTab === 'code'" class="h-full flex flex-col">
            <!-- Editor section - exactly half height, fixed and non-scrollable -->
            <div class="h-1/2 p-4 flex flex-col border-b">
              <ClientOnly>
                <div class="flex-1 min-h-0">
                  <MonacoEditor
                    v-model="editorCode"
                    lang="python"
                    :options="{ theme: 'vs-dark', automaticLayout: false, minimap: { enabled: false }, wordWrap: 'on' }"
                    style="height: 100%"
                  />
                </div>
              </ClientOnly>
              <div v-if="errorMsg" class="mt-2 text-xs text-red-600">{{ errorMsg }}</div>
              <div class="mt-3 flex items-center justify-end space-x-2">
                <button class="px-3 py-1.5 text-xs rounded bg-gray-800 text-white hover:bg-gray-700" :disabled="running" @click="runNewStep">
                  <span v-if="running && runMode === 'save'">Saving…</span>
                  <span v-else>
                    Save</span>
                </button>
                <button class="px-3 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50 flex items-center" :disabled="running" @click="previewRun">
                  <span v-if="running && runMode === 'preview'">Running…</span>
                  <span v-else class="flex items-center">
                    <Icon name="heroicons-play" class="w-3 h-3 mr-1.5" />
                    Run</span>
                </button>

              </div>
            </div>
            <!-- Results section - exactly half height, scrollable -->
            <div class="h-1/2 p-4 flex flex-col min-h-0">
              <div class="text-xs text-gray-600 mb-2 flex-shrink-0" v-if="preview?.info">Rows: {{ preview.info.total_rows?.toLocaleString?.() || preview.info.total_rows }}</div>
              <div class="flex-1 overflow-auto min-h-0">
                <div v-if="preview && preview.columns && preview.rows" class="border rounded">
                  <table class="min-w-full text-xs">
                    <thead class="bg-gray-50 sticky top-0">
                      <tr>
                        <th v-for="col in preview.columns" :key="col.field" class="px-2 py-1 text-left font-medium text-gray-600">{{ col.headerName || col.field }}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(row, rIdx) in preview.rows" :key="rIdx" class="border-t">
                        <td v-for="col in preview.columns" :key="col.field" class="px-2 py-1 text-gray-800">
                          {{ row[col.field] }}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div v-else class="text-xs" :class="errorMsg ? 'text-red-600' : 'text-gray-400'">{{ errorMsg || 'No preview yet.' }}</div>
              </div>
            </div>
          </div>

          <div v-else class="flex-1 overflow-auto">
            <div v-if="visualizations.length > 0" class="h-full">
              <!-- Visualization Cards -->
              <div class="space-y-4 p-4">
                <div v-for="viz in visualizations" :key="viz.id" class="border border-gray-200 rounded-lg bg-white overflow-hidden">
                  <!-- Header -->
                  <div class="px-3 py-2 border-b border-gray-100 bg-gray-50">
                    <div class="flex items-center justify-between">
                      <div class="flex items-center space-x-2">
                        <h4 class="text-sm font-medium text-gray-800">{{ viz.title || 'Untitled' }}</h4>
                      </div>
                    </div>
                  </div>
                  
                  <!-- Content: Visualization (60%) + Config (40%) -->
                  <div class="flex h-[32rem]">
                    <!-- Left: Visualization Rendering (60%) -->
                    <div class="w-[65%] border-r border-gray-100">
                      <div v-if="shouldShowVisual(viz)" class="h-full bg-gray-50 p-2">
                        <!-- Dynamic component rendering -->
                        <div v-if="getResolvedCompEl(viz) && shouldShowVisual(viz)" class="h-full">
                          <component
                            :is="getResolvedCompEl(viz)"
                            :key="getChartKey(viz)"
                            :widget="{ id: viz.id, title: viz.title } as any"
                            :data="viz.step?.data"
                            :data_model="getDataModelWithView(viz)"
                            :step="viz.step"
                            :view="wrapView(viz.view)"
                            :reportThemeName="reportThemeName"
                            :reportOverrides="reportOverrides"
                          />
                        </div>
                        <!-- Fallback rendering -->
                        <div v-else-if="chartVisualTypes.has(viz.view?.type || viz.step?.data_model?.type)" class="h-full">
                          <RenderVisual 
                            :key="getChartKey(viz)"
                            :widget="{ id: viz.id, title: viz.title } as any" 
                            :data="viz.step?.data" 
                            :data_model="getDataModelWithView(viz)"
                            :view="wrapView(viz.view)"
                          />
                        </div>
                        <div v-else-if="(viz.view?.type || viz.step?.data_model?.type) === 'count'" class="h-full flex items-center justify-center">
                          <RenderCount 
                            :widget="{ id: viz.id, title: viz.title } as any" 
                            :data="viz.step?.data" 
                            :data_model="getDataModelWithView(viz)" 
                          />
                        </div>
                        <div v-else class="h-full flex items-center justify-center text-gray-400 text-sm">No preview available</div>
                      </div>
                      <div v-else class="h-full bg-gray-50 p-2">
                        <div v-if="String(viz.view?.type || viz.step?.data_model?.type).toLowerCase() === 'table'" class="h-full">
                          <RenderTable 
                            :widget="{ id: viz.id, title: viz.title } as any" 
                            :step="{ ...(viz.step || {}), data_model: getDataModelWithView(viz) } as any" 
                          />
                        </div>
                        <div v-else class="h-full flex items-center justify-center text-gray-400 text-sm">No visual representation</div>
                      </div>
                    </div>
                    
                    <!-- Right: Configuration (40%) -->
                    <div class="w-[35%] p-3 flex flex-col overflow-x-hidden">
                      <VisualizationConfigEditor
                        :viz="viz"
                        :step="viz.step"
                        @apply="onVizApply(viz, $event)"
                        @saved="onVizSaved"
                      />
                    </div>
                  </div>
                </div>
              </div>
            </div>
            
            <div v-else class="flex-1 flex items-center justify-center">
              <div class="text-center py-8">
                <div v-if="loadingVisualizations" class="flex flex-col items-center">
                  <Spinner class="w-4 h-4 mb-2" />
                  <div class="text-gray-400 text-sm">Loading visualizations...</div>
                </div>
                <div v-else>
                  <div class="text-gray-400 text-sm">No visualizations found for this query</div>
                  <div v-if="!queryId" class="text-xs text-gray-400 mt-1">Query ID not available</div>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, computed, defineAsyncComponent } from 'vue'
import { useMyFetch } from '~/composables/useMyFetch'
import RenderVisual from '../RenderVisual.vue'
import RenderCount from '../RenderCount.vue'
import RenderTable from '../RenderTable.vue'
import Spinner from '../Spinner.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'
import VisualizationConfigEditor from './VisualizationConfigEditor.vue'
import { useOrgSettings } from '~/composables/useOrgSettings'

interface Props {
  visible: boolean
  queryId?: string | null
  initialCode: string
  title: string
  stepId?: string | null
  toolExecutionId?: string | null
}

const props = defineProps<Props>()
const route = useRoute()
const emit = defineEmits(['close', 'stepCreated'])

const editorCode = ref('')
const running = ref(false)
const errorMsg = ref('')
const preview = ref<any | null>(null)
const queryId = ref<string | null>(null)
const currentStepId = ref<string | null>(null)
const queryData = ref<any | null>(null)
const visualizations = ref<any[]>([])
const loadingVisualizations = ref(false)
const reportThemeName = ref<string | null>(null)
const reportOverrides = ref<Record<string, any> | null>(null)

const open = computed({
  get: () => props.visible,
  set: (v: boolean) => {
    if (!v) emit('close')
  }
})

const { canEditCode } = useOrgSettings()

const activeTab = ref<'code' | 'visuals'>(canEditCode.value ? 'code' : 'visuals')

watch(canEditCode, (v) => {
  if (!v) activeTab.value = 'visuals'
})

// Keep internal queryId in sync with prop while modal is open
watch(() => props.queryId, (v) => {
  if (props.visible && v) {
    queryId.value = v
  }
})

// Reload query data when queryId changes
watch(() => queryId.value, async (newQueryId) => {
  if (newQueryId && props.visible) {
    await loadQueryData()
  }
})

async function syncQueryIdOnOpen() {
  queryId.value = props.queryId || null
  if (!queryId.value && props.stepId) {
    try {
      const s: any = await useMyFetch(`/api/steps/${props.stepId}`)
      const step = s?.data?.value
      if (step?.query_id) queryId.value = step.query_id
    } catch {
      // ignore
    }
  }
}

async function loadQueryData() {
  if (!queryId.value) return
  
  loadingVisualizations.value = true
  try {
    const { data, error } = await useMyFetch(`/api/queries/${queryId.value}`)
    if (error.value) throw error.value
    
    queryData.value = data.value
    // Load theme from owning report (queries themselves do not carry theme)
    try {
      const rid = (data.value as any)?.report_id
      if (rid) {
        const { data: rdata, error: rerr } = await useMyFetch(`/api/reports/${rid}`)
        if (!rerr.value) {
          const rpt: any = rdata.value
          reportThemeName.value = rpt?.report_theme_name || rpt?.theme_name || null
          reportOverrides.value = rpt?.theme_overrides || null
        }
      }
    } catch {}
    const vizList = (data.value as any)?.visualizations || []
    const defaultStep = (data.value as any)?.default_step
    
    // Attach the default step to each visualization
    const vizWithSteps = vizList.map((viz: any) => ({
      ...viz, 
      step: defaultStep
    }))
    
    visualizations.value = vizWithSteps
  } catch (e) {
    console.error('Failed to load query data:', e)
    queryData.value = null
    visualizations.value = []
  } finally {
    loadingVisualizations.value = false
  }
}

// Ensure we have a query id; create one if missing
async function ensureQueryId() {
  if (queryId.value) return
  // Try to sync from props/step first
  await syncQueryIdOnOpen()
  if (queryId.value) return
  // Create a new query with the provided title
  const resp: any = await useMyFetch('/api/queries', {
    method: 'POST',
    body: { title: props.title, report_id: (route?.params as any)?.id || null }
  })
  const q = resp?.data?.value
  if (!q?.id) {
    throw new Error('Failed to create query.')
  }
  queryId.value = q.id
}

watch(() => props.visible, async (v) => {
  if (v) {
    editorCode.value = props.initialCode || ''
    errorMsg.value = ''
    preview.value = null
    currentStepId.value = props.stepId || null
    if (!canEditCode.value) activeTab.value = 'visuals'
    await syncQueryIdOnOpen()
    await loadQueryData()
    await loadInitialStepOrDefault()
    // Listen for theme changes to update editor visuals
    try {
      const handler = (ev: any) => {
        const d = (ev && ev.detail) || {}
        if (!d) return
        reportThemeName.value = d.themeName || reportThemeName.value
        reportOverrides.value = d.overrides || reportOverrides.value
      }
      window.addEventListener('dashboard:theme_changed', handler as any, { once: true })
    } catch {}
  }
})

onMounted(async () => {
  if (props.visible) {
    editorCode.value = props.initialCode || ''
    currentStepId.value = props.stepId || null
    await syncQueryIdOnOpen()
    await loadQueryData()
    await loadInitialStepOrDefault()
  }
})

async function loadInitialStepOrDefault() {
  try {
    let loadedStep: any = null
    // If a stepId is provided, load it first
    if (props.stepId) {
      const s: any = await useMyFetch(`/api/steps/${props.stepId}`)
      loadedStep = s?.data?.value || null
    }
    // If we have a queryId, fetch the current default step
    let defaultStep: any = null
    if (queryId.value) {
      const resp: any = await useMyFetch(`/api/queries/${queryId.value}/default_step`)
      defaultStep = resp?.data?.value?.step || null
    }
    const step = defaultStep || loadedStep
    if (step) {
      currentStepId.value = step.id
      if (step.query_id && !queryId.value) queryId.value = step.query_id
      if (step.data) preview.value = step.data
      if (!editorCode.value && step.code) editorCode.value = step.code
    }
  } catch (e) {
    // swallow
  }
}

const runMode = ref<'preview' | 'save' | null>(null)

// Visualization rendering logic (similar to ToolWidgetPreview)
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

// Get the resolved component for a visualization
function getResolvedCompEl(viz: any) {
  const vType = viz?.view?.type
  const dmType = viz?.step?.data_model?.type
  return getCompForType(String(vType || dmType || ''))
}

// Check if visualization should show visual rendering
function shouldShowVisual(viz: any) {
  const t = viz?.view?.type || viz?.step?.data_model?.type
  if (!t) return false
  const entry = resolveEntryByType(String(t).toLowerCase())
  if (entry) {
    // treat table as data-only; everything else is a visual
    return entry.componentKey !== 'table.aggrid'
  }
  return chartVisualTypes.has(String(t)) || String(t) === 'count'
}

// Helper to wrap flat view for RenderVisual which expects { view: {...} }
function wrapView(view: any) {
  if (!view) return null
  // If already wrapped (has .view property with type), return as-is
  if (view.view?.type) return view
  // Wrap flat view
  return { view, version: 'v2' }
}

// Helper to merge view type into data_model for rendering
function getDataModelWithView(viz: any) {
  const dm = viz.step?.data_model || {}
  const viewType = viz.view?.type
  if (viewType && viewType !== dm.type) {
    return { ...dm, type: viewType }
  }
  return dm
}

// Generate a key for the chart component that changes when view settings change
function getChartKey(viz: any): string {
  const v = viz.view || {}
  // Include all view fields that affect rendering to force re-render when they change
  return `${viz.id}-${v.type || ''}-${v.x || ''}-${JSON.stringify(v.y || '')}-${v.category || ''}-${v.value || ''}-${JSON.stringify(v.encoding || {})}-${JSON.stringify(v.legend || {})}-${JSON.stringify(v.axisX || {})}-${JSON.stringify(v.axisY || {})}-${v.showGrid}-${v.stacked}-${v.smooth}-${v.horizontal}-${v.donut}`
}

function onVizApply(viz: any, nextView: any) {
  // Replace the view entirely (not merge) to ensure all fields are from the new payload
  const updated = { ...(nextView || {}) }
  
  // Force reactivity update on the visualizations array by replacing the object
  const idx = visualizations.value.findIndex((v: any) => v.id === viz.id)
  if (idx >= 0) {
    // Create a completely new object to ensure Vue detects the change
    visualizations.value[idx] = { 
      ...visualizations.value[idx], 
      view: updated 
    }
    // Also update the original viz reference (for v-for iteration)
    viz.view = updated
  }
}

function onVizSaved(updated: any) {
  if (!updated?.id) return
  const idx = visualizations.value.findIndex(v => v.id === updated.id)
  if (idx >= 0) {
    // Preserve attached step for preview while merging new view/title/status
    const prev = visualizations.value[idx]
    visualizations.value[idx] = { ...prev, ...updated, step: prev.step }
  }
  // Broadcast so dashboard can update the rendered tile immediately
  try {
    window.dispatchEvent(new CustomEvent('visualization:updated', { detail: { id: updated.id, visualization: updated } }))
  } catch {}
}

async function previewRun() {
  running.value = true
  runMode.value = 'preview'
  errorMsg.value = ''
  try {
    // Ensure we have or create a query id
    await ensureQueryId()
    const resp: any = await useMyFetch(`/api/queries/${queryId.value}/preview`, {
      method: 'POST',
      body: { code: editorCode.value, title: props.title, type: 'table', tool_execution_id: props.toolExecutionId || null }
    })
    const payload = resp?.data?.value
    if (payload?.error) {
      errorMsg.value = payload.error
      preview.value = null
      return
    }
    preview.value = payload?.preview || null
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || 'Failed to run'
  } finally {
    running.value = false
    runMode.value = null
  }
}

async function runNewStep() {
  running.value = true
  runMode.value = 'save'
  errorMsg.value = ''
  try {
    // Ensure we have or create a query id
    await ensureQueryId()
    const resp: any = await useMyFetch(`/api/queries/${queryId.value}/run`, {
      method: 'POST',
      body: {
        code: editorCode.value,
        title: props.title,
        type: 'table',
        tool_execution_id: props.toolExecutionId || null
      }
    })
    const payload = resp?.data?.value
    // Show backend error message if execution failed
    if (payload?.error) {
      errorMsg.value = payload.error
      preview.value = null
      return
    }
    preview.value = payload?.step?.data || null
    if (payload?.step) {
      currentStepId.value = payload.step.id
      emit('stepCreated', payload.step)
      // Broadcast the new default step so dashboard can refresh the tile immediately
      try {
        const step: any = payload.step
        const qid = step?.query_id || queryId.value
        if (qid) {
          window.dispatchEvent(new CustomEvent('query:default_step_changed', { detail: { query_id: qid, step } }))
          // Also instruct the originating chat preview (if any) to rebind to this query id
          if (props.toolExecutionId) {
            window.dispatchEvent(new CustomEvent('tool_preview:rebind', { detail: { tool_execution_id: props.toolExecutionId, query_id: qid } }))
          }
        }
      } catch {}
    }
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || 'Failed to run'
  } finally {
    running.value = false
    runMode.value = null
  }
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>



