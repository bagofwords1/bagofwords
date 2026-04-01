<template>
  <div class="mb-2">
    <!-- Main Header: Creating Data (always collapsible) -->
    <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCreateData">
      <Icon :name="createDataCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Spinner v-if="status === 'running'" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Icon v-else-if="status === 'success'" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
      <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Icon v-else-if="status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 mr-1.5 text-amber-500" />
      <span v-if="status === 'running'" class="tool-shimmer">Creating Data</span>
      <span v-else-if="status === 'success'" class="text-gray-700">Created Data</span>
      <span v-else-if="status === 'stopped'" class="text-gray-700 italic">Creating Data</span>
      <span v-else-if="status === 'error'" class="text-gray-700">Create Data</span>
      <span v-else class="text-gray-700">Create Data</span>
      <span v-if="formatDuration" class="ml-1.5 text-gray-400">{{ formatDuration }}</span>
    </div>

    <!-- Stopped/Error message below header -->
    <div v-if="status === 'stopped'" class="mt-1 ml-4 text-xs text-gray-400 italic">Generation stopped</div>
    <div v-else-if="status === 'error' && lastErrorMessage" class="mt-1 ml-4 text-xs text-gray-500">
      {{ lastErrorMessage }}
    </div>

    <!-- Collapsible content: Generating Code, Executing, Visualizing sections -->
    <Transition name="fade">
      <div v-if="!createDataCollapsed" class="mt-2 ml-4 space-y-2">
        <!-- Generating Code section -->
        <div>
          <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click.stop="toggleCode">
            <Spinner v-if="isCodeGenerating && status !== 'stopped'" class="w-3 h-3 mr-1.5 text-gray-400" />
            <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 mr-1.5 text-gray-400" />
            <Icon v-else-if="status === 'error' && !codeContent" name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-gray-400" />
            <Icon v-else-if="codeGenDone" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
            <Icon v-else name="heroicons-minus" class="w-3 h-3 mr-1.5 text-gray-300" />
            <span v-if="isCodeGenerating && status !== 'stopped'" class="tool-shimmer">Generating Code</span>
            <span v-else class="text-gray-700">Generated Code</span>
            <span v-if="currentAttempt > 1 && isCodeGenerating" class="ml-1.5 text-gray-400">· attempt {{ currentAttempt }}</span>
            <Icon :name="codeCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 ml-2" />
          </div>
          <Transition name="fade">
            <div v-if="!codeCollapsed && codeContent" class="mt-1 ml-4">
              <div class="bg-gray-50 rounded px-4 py-3 font-mono text-xs max-h-42 overflow-y-auto relative">
                <button
                  class="absolute top-2 right-2 px-2 py-1 text-xs rounded border border-gray-300 bg-transparent text-gray-600 hover:bg-gray-100 hover:text-gray-800"
                  :disabled="!canOpenEditor"
                  v-if="canOpenEditor && !readonly"
                  @click.stop="openEditor"
                >
                  Edit code
                </button>
                <pre class="text-gray-800 whitespace-pre-wrap pr-20">{{ codeContent }}</pre>
              </div>
            </div>
          </Transition>
        </div>

        <!-- Executing Code section -->
        <div v-if="showExecutingSection">
          <div class="flex items-center text-xs text-gray-500">
            <Spinner v-if="isExecuting && status !== 'stopped'" class="w-3 h-3 mr-1.5 text-gray-400" />
            <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 mr-1.5 text-gray-400" />
            <Icon v-else-if="executionFailed" name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-amber-500" />
            <Icon v-else-if="executionDone" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
            <Icon v-else name="heroicons-minus" class="w-3 h-3 mr-1.5 text-gray-300" />
            <span v-if="isExecuting && status !== 'stopped'" class="tool-shimmer">Executing</span>
            <span v-else-if="executionFailed" class="text-gray-700">Execution Failed</span>
            <span v-else class="text-gray-700">Executed</span>
          </div>
          <!-- Execution error from stdout -->
          <div v-if="latestStdoutError" class="mt-1 ml-4 text-[11px] text-amber-600 bg-amber-50/50 rounded px-2 py-1 max-h-16 overflow-y-auto">
            <pre class="whitespace-pre-wrap break-words m-0">{{ latestStdoutError }}</pre>
          </div>
        </div>

        <!-- Retry indicator -->
        <div v-if="isRetrying" class="flex items-center text-xs text-gray-500">
          <Spinner class="w-3 h-3 mr-1.5 text-gray-400" />
          <span class="tool-shimmer">Retrying · attempt {{ currentAttempt }}</span>
        </div>

        <!-- Visualizing section -->
        <div v-if="showVisualizingSection">
          <div class="flex items-center text-xs text-gray-500">
            <Spinner v-if="isVisualizing && status !== 'stopped'" class="w-3 h-3 mr-1.5 text-gray-400" />
            <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 mr-1.5 text-gray-400" />
            <Icon v-else-if="vizError" name="heroicons-exclamation-circle" class="w-3 h-3 mr-1.5 text-amber-500" />
            <Icon v-else-if="vizDone" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
            <span v-if="isVisualizing && status !== 'stopped'" class="tool-shimmer">Visualizing</span>
            <span v-else-if="vizError" class="text-gray-700">Visualizing</span>
            <span v-else class="text-gray-700">{{ chartTypeLabel }}</span>
            <span v-if="vizSummary && !vizError" class="ml-1.5 text-gray-400">· {{ vizSummary }}</span>
          </div>
          <div v-if="vizError" class="mt-1 ml-4 text-xs text-gray-500">{{ vizError }}</div>
        </div>
      </div>
    </Transition>

    <!-- Results Preview - only show if not failed -->
    <div class="mt-2" v-if="hasPreview && status !== 'error'">
      <ToolWidgetPreview :tool-execution="toolExecution" :readonly="readonly" @addWidget="onAddWidget" @toggleSplitScreen="$emit('toggleSplitScreen')" @editQuery="$emit('editQuery', $event)" />
    </div>
  </div>
  <QueryCodeEditorModal
    :visible="showEditor"
    :query-id="createdQueryId"
    :initial-code="codeContent || ''"
    :title="dataTitle"
    :step-id="initialStepId"
    :tool-execution-id="props.toolExecution?.id || null"
    @close="showEditor = false"
    @stepCreated="onModalSaved"
  />
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'
import QueryCodeEditorModal from '~/components/tools/QueryCodeEditorModal.vue'
import Spinner from '~/components/Spinner.vue'

interface Props {
  toolExecution: {
    id: string
    tool_name: string
    tool_action?: string
    arguments_json?: {
      title?: string
      user_prompt?: string
      interpreted_prompt?: string
    }
    result_json?: {
      code?: string
      data?: any
      stats?: {
        total_rows?: number
      }
    }
    status: string
    result_summary?: string
    duration_ms?: number
    created_widget_id?: string
    created_step_id?: string
    created_widget?: any
    created_step?: any
  }
}

const props = defineProps<Props & { readonly?: boolean }>()
const emit = defineEmits(['addWidget', 'toggleSplitScreen', 'editQuery'])

const codeCollapsed = ref(false)
const createDataCollapsed = ref(true) // Collapsed by default
const dataTitle = computed(() => props.toolExecution.arguments_json?.title || 'Data')
const status = computed(() => props.toolExecution.status)
const progressStage = computed(() => (props.toolExecution as any).progress_stage || '')

// Code content: prefer final result, fall back to streamed progress code
const codeContent = computed(() =>
  props.toolExecution?.created_step?.code
  || props.toolExecution.result_json?.code
  || (props.toolExecution as any).progress_code
  || ''
)
const resultSummary = computed(() => props.toolExecution.result_summary)
const successDetails = computed(() => {
  if (status.value !== 'success') return null
  const totalRows = (props.toolExecution as any)?.result_json?.stats?.total_rows
    || (props.toolExecution as any)?.result_json?.data?.info?.total_rows
    || (props.toolExecution as any)?.result_json?.widget_data?.info?.total_rows
  return totalRows !== undefined ? `${Number(totalRows).toLocaleString()} rows` : null
})
const attempts = computed(() => {
  const errs = (props.toolExecution.result_json as any)?.errors || []
  return errs.map((pair: any) => {
    const msg = Array.isArray(pair) ? pair[1] : (pair?.message || String(pair))
    const firstLine = (msg || '').split('\n')[0]
    return firstLine
  })
})
const lastErrorMessage = computed(() => attempts.value?.[attempts.value.length - 1] || '')
const currentAttempt = computed(() => {
  const pa = (props.toolExecution as any).progress_attempt
  if (typeof pa === 'number') return pa + 1
  const len = attempts.value?.length || 0
  return len > 0 ? len + 1 : 1
})
const hasPreview = computed(() => {
  const te: any = props.toolExecution
  const hasObject = !!(te?.created_widget || te?.created_step)
  const hasViz = Array.isArray(te?.created_visualizations) && te.created_visualizations.length > 0
  const hasQuery = !!(te?.result_json?.query_id)
  const hasRows = Array.isArray(te?.result_json?.data?.rows) || Array.isArray(te?.result_json?.widget_data?.rows)
  return !!(hasObject || hasViz || hasQuery || hasRows)
})

// Stage-based state
const isCodeGenerating = computed(() => progressStage.value === 'generating_code')
const codeGenDone = computed(() => !!codeContent.value && !isCodeGenerating.value)
const isExecuting = computed(() => progressStage.value === 'executing_code')
const isRetrying = computed(() => progressStage.value === 'retry')
const executionDone = computed(() => {
  // Execution is done if we've moved past executing_code to a later stage or finished
  if (status.value === 'success') return true
  const pastExecution = ['inferring_visualization', 'formatting_widget', 'visualization_inferred', 'series_configured'].includes(progressStage.value)
  return pastExecution
})
const executionFailed = computed(() => {
  // Failed if status is error OR if we're retrying (meaning previous execution failed)
  if (status.value === 'error') return true
  if (isRetrying.value) return true
  return false
})

// Show executing section once code generation is done
const showExecutingSection = computed(() => {
  if (status.value === 'stopped') return false
  const pastCodeGen = ['generated_code', 'executing_code', 'retry', 'inferring_visualization', 'formatting_widget', 'visualization_inferred', 'series_configured'].includes(progressStage.value)
  return pastCodeGen || isExecuting.value || executionDone.value || (status.value === 'error' && codeContent.value)
})

// Stdout errors from progress_stdout
const stdoutMessages = computed(() => (props.toolExecution as any).progress_stdout || [])
const latestStdoutError = computed(() => {
  if (!stdoutMessages.value.length) return ''
  // Show the most recent error, trim to first meaningful line
  const last = stdoutMessages.value[stdoutMessages.value.length - 1] || ''
  const firstLine = last.split('\n')[0]
  return firstLine.length > 200 ? firstLine.slice(0, 200) + '…' : firstLine
})

// Visualization state
const isVisualizing = computed(() => progressStage.value === 'inferring_visualization')
const vizInferredData = computed(() => (props.toolExecution as any).progress_visualization || null)
const vizError = computed(() => (props.toolExecution as any).progress_visualization_error || null)
const vizDone = computed(() => {
  const fromProgress = vizInferredData.value
  const fromResult = props.toolExecution.result_json as any
  return !!(fromProgress || fromResult?.data_model?.type)
})
const showVisualizingSection = computed(() => {
  const resultType = (props.toolExecution.result_json as any)?.data_model?.type
  return isVisualizing.value || vizError.value || (vizDone.value && resultType && resultType !== 'table')
})

// Chart type display
const chartType = computed(() => {
  const fromProgress = vizInferredData.value?.chart_type
  const fromResult = (props.toolExecution.result_json as any)?.data_model?.type
  return fromProgress || fromResult || ''
})
const chartTypeLabel = computed(() => {
  const typeMap: Record<string, string> = {
    bar_chart: 'Bar Chart',
    line_chart: 'Line Chart',
    area_chart: 'Area Chart',
    pie_chart: 'Pie Chart',
    scatter_chart: 'Scatter Plot',
    metric_card: 'Metric Card',
    table: 'Table',
    grouped_bar_chart: 'Grouped Bar',
    stacked_bar_chart: 'Stacked Bar',
    stacked_area_chart: 'Stacked Area'
  }
  return typeMap[chartType.value] || chartType.value
})

// Simple visualization summary (e.g., "x → y")
const vizSummary = computed(() => {
  const series = vizInferredData.value?.series || (props.toolExecution.result_json as any)?.data_model?.series || []
  if (!series.length) return ''
  const s = series[0]
  if (s.x && s.y) return `${s.x} → ${s.y}`
  if (s.key && s.value) return `${s.key} → ${s.value}`
  if (s.value) return s.value
  return ''
})

const formatDuration = computed(() => {
  if (!props.toolExecution.duration_ms) return ''
  const seconds = (props.toolExecution.duration_ms / 1000).toFixed(1)
  return `${seconds}s`
})

watch([codeGenDone, status], ([codeNow, st]) => {
  // Keep code section collapsed by default
  if (st === 'error' || st === 'success') {
    codeCollapsed.value = true
  }
}, { immediate: true })

function toggleCode() { codeCollapsed.value = !codeCollapsed.value }
function toggleCreateData() { createDataCollapsed.value = !createDataCollapsed.value }
function onAddWidget(payload: { widget?: any, step?: any }) { emit('addWidget', payload) }

const initialStepId = computed(() => props.toolExecution?.created_step_id || props.toolExecution?.created_step?.id || null)
const createdQueryId = computed(() => {
  const stepQ = (props.toolExecution?.created_step as any)?.query_id
  if (stepQ) return stepQ
  const resultQ = (props.toolExecution as any)?.result_json?.query_id
  return resultQ || null
})
const canOpenEditor = computed(() => !!(initialStepId.value || createdQueryId.value || codeContent.value))
async function openEditor() { if (!canOpenEditor.value) return; showEditor.value = true }
const showEditor = ref(false)
function onModalSaved(step: any) {
  (props.toolExecution as any).created_step_id = step?.id
  ;(props.toolExecution as any).created_step = step
  emit('addWidget', { step })
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active { transition: opacity 0.3s ease; }
.fade-enter-from,
.fade-leave-to { opacity: 0; }
@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
  font-weight: 400;
  opacity: 1;
}
</style>
