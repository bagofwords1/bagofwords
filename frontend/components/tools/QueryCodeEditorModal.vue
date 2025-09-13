<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-6xl', height: 'sm:h-[90vh]' }">
    <div class="h-full flex flex-col">
      <div class="px-4 py-3 border-b flex items-center justify-between flex-shrink-0">
        <div class="text-sm font-medium text-gray-800">Edit code — {{ title }}</div>
        <div v-if="currentStepId" class="ml-4 text-[11px] text-gray-500">Query ID: {{ queryId }}</div>
        <button class="px-3 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50" @click="open = false">Close</button>
      </div>
      <div class="flex-1 flex overflow-hidden min-h-0">
        <!-- Left tabs -->
        <aside class="w-40 border-r">
          <nav class="p-2 text-sm">
            <button
              class="w-full text-left px-2 py-1.5 rounded mb-1 transition-colors"
              :class="activeTab === 'code' ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'"
              @click="activeTab = 'code'"
            >Code</button>
            <button
              class="w-full text-left px-2 py-1.5 rounded transition-colors"
              :class="activeTab === 'visuals' ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'"
              @click="activeTab = 'visuals'"
            >Visuals</button>
          </nav>
        </aside>

        <!-- Right content -->
        <section class="flex-1 flex flex-col overflow-hidden min-h-0">
          <div v-if="activeTab === 'code'" class="h-full flex flex-col">
            <!-- Editor section - exactly half height, fixed and non-scrollable -->
            <div class="h-1/2 p-4 flex flex-col border-b">
              <textarea
                v-model="editorCode"
                class="block w-full flex-1 resize-none font-mono text-xs border rounded p-3 text-gray-800 focus:outline-none focus:ring-1 focus:ring-gray-300 min-h-0"
                spellcheck="false"
              />
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
                <div v-else class="text-xs text-gray-400">No preview yet.</div>
              </div>
            </div>
          </div>

          <div v-else class="flex-1 overflow-auto">
            <div v-if="visualizations.length > 0" class="h-full">
              <div class="p-4 border-b">
                <h3 class="text-sm font-medium text-gray-800">Visualizations ({{ visualizations.length }})</h3>
              </div>
              
              <!-- Visualization Cards -->
              <div class="space-y-4 p-4">
                <div v-for="viz in visualizations" :key="viz.id" class="border border-gray-200 rounded-lg bg-white overflow-hidden">
                  <!-- Header -->
                  <div class="px-3 py-2 border-b border-gray-100 bg-gray-50">
                    <div class="flex items-center justify-between">
                      <div class="flex items-center space-x-2">
                        <h4 class="text-sm font-medium text-gray-800">{{ viz.title || 'Untitled' }}</h4>
                        <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium"
                              :class="viz.status === 'success' ? 'bg-green-100 text-green-800' : 
                                     viz.status === 'draft' ? 'bg-gray-100 text-gray-800' :
                                     'bg-red-100 text-red-800'">
                          {{ viz.status }}
                        </span>
                      </div>
                      <div class="text-xs text-gray-500">
                        ID: {{ viz.id }}
                      </div>
                    </div>
                  </div>
                  
                  <!-- Content: Visualization (2/3) + Config (1/3) -->
                  <div class="flex h-80">
                    <!-- Left: Visualization Rendering (2/3) -->
                    <div class="w-2/3 border-r border-gray-100">
                      <div v-if="shouldShowVisual(viz)" class="h-full bg-gray-50 p-2">
                        <!-- Dynamic component rendering -->
                        <div v-if="getResolvedCompEl(viz)" class="h-full">
                          <component
                            :is="getResolvedCompEl(viz)"
                            :widget="{ id: viz.id, title: viz.title } as any"
                            :data="viz.step?.data"
                            :data_model="viz.step?.data_model"
                            :step="viz.step"
                            :view="viz.view"
                          />
                        </div>
                        <!-- Fallback rendering -->
                        <div v-else-if="chartVisualTypes.has(viz.view?.type || viz.step?.data_model?.type)" class="h-full">
                          <RenderVisual 
                            :widget="{ id: viz.id, title: viz.title } as any" 
                            :data="viz.step?.data" 
                            :data_model="viz.step?.data_model" 
                          />
                        </div>
                        <div v-else-if="(viz.view?.type || viz.step?.data_model?.type) === 'count'" class="h-full flex items-center justify-center">
                          <RenderCount 
                            :show_title="true" 
                            :widget="{ id: viz.id, title: viz.title } as any" 
                            :data="viz.step?.data" 
                            :data_model="viz.step?.data_model" 
                          />
                        </div>
                        <div v-else class="h-full flex items-center justify-center text-gray-400 text-sm">
                          No preview available
                        </div>
                      </div>
                      <div v-else class="h-full flex items-center justify-center text-gray-400 text-sm">
                        No visual representation
                      </div>
                    </div>
                    
                    <!-- Right: Configuration (1/3) -->
                    <div class="w-1/3 p-3 overflow-auto">
                      <div v-if="viz.view" class="text-xs text-gray-600 space-y-3">
                        <div v-if="viz.view.type">
                          <div class="font-medium text-gray-800 mb-1">Type</div>
                          <div class="text-gray-600">{{ viz.view.type }}</div>
                        </div>
                        <div v-if="viz.view.encoding">
                          <div class="font-medium text-gray-800 mb-1">Encoding</div>
                          <pre class="p-2 bg-gray-50 rounded text-xs overflow-auto max-h-48">{{ JSON.stringify(viz.view.encoding, null, 2) }}</pre>
                        </div>
                        <div v-if="Object.keys(viz.view).length > 2">
                          <div class="font-medium text-gray-800 mb-1">Full Configuration</div>
                          <pre class="p-2 bg-gray-50 rounded text-xs overflow-auto max-h-32">{{ JSON.stringify(viz.view, null, 2) }}</pre>
                        </div>
                      </div>
                      
                      <div v-else class="text-xs text-gray-400 italic">
                        No view configuration
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            
            <div v-else class="flex-1 flex items-center justify-center">
              <div class="text-center py-8">
                <div class="text-gray-400 text-sm">No visualizations found for this query</div>
                <div v-if="!queryId" class="text-xs text-gray-400 mt-1">Query ID not available</div>
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
import { resolveEntryByType } from '@/components/dashboard/registry'

interface Props {
  visible: boolean
  queryId?: string | null
  initialCode: string
  title: string
  stepId?: string | null
  toolExecutionId?: string | null
}

const props = defineProps<Props>()
const emit = defineEmits(['close', 'stepCreated'])

const editorCode = ref('')
const running = ref(false)
const errorMsg = ref('')
const preview = ref<any | null>(null)
const queryId = ref<string | null>(null)
const currentStepId = ref<string | null>(null)
const queryData = ref<any | null>(null)
const visualizations = ref<any[]>([])

const open = computed({
  get: () => props.visible,
  set: (v: boolean) => {
    if (!v) emit('close')
  }
})

const activeTab = ref<'code' | 'visuals'>('code')

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
  
  try {
    const { data, error } = await useMyFetch(`/api/queries/${queryId.value}`)
    if (error.value) throw error.value
    
    queryData.value = data.value
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
  }
}

watch(() => props.visible, async (v) => {
  if (v) {
    editorCode.value = props.initialCode || ''
    errorMsg.value = ''
    preview.value = null
    currentStepId.value = props.stepId || null
    await syncQueryIdOnOpen()
    await loadQueryData()
    await loadInitialStepOrDefault()
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

async function previewRun() {
  running.value = true
  runMode.value = 'preview'
  errorMsg.value = ''
  try {
    // Ensure we have a query id at click-time
    if (!queryId.value) {
      await syncQueryIdOnOpen()
    }
    if (!queryId.value) {
      throw new Error('Query not found.')
    }
    const resp: any = await useMyFetch(`/api/queries/${queryId.value}/preview`, {
      method: 'POST',
      body: { code: editorCode.value, title: props.title, type: 'table' }
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
    // Ensure we have a query id at click-time
    if (!queryId.value) {
      await syncQueryIdOnOpen()
    }
    if (!queryId.value) {
      throw new Error('Query not found.')
    }
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


