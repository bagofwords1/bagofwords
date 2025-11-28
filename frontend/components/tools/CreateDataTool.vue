<template>
  <div class="mb-2">
    <!-- Main Header: Creating Data (always collapsible) -->
    <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCreateData">
      <Icon :name="createDataCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Spinner v-if="status === 'running'" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Icon v-else-if="status === 'success'" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 mr-1.5 text-amber-500" />
      <span v-if="status === 'running'" class="tool-shimmer">Creating Data</span>
      <span v-else-if="status === 'success'" class="text-gray-700">Created Data</span>
      <span v-else-if="status === 'error'" class="text-gray-700">Create Data</span>
      <span v-else class="text-gray-700">Create Data</span>
      <span v-if="formatDuration" class="ml-1.5 text-gray-400">{{ formatDuration }}</span>
    </div>

    <!-- Error message below header -->
    <div v-if="status === 'error' && lastErrorMessage" class="mt-1 ml-4 text-xs text-gray-500">
      {{ lastErrorMessage }}
    </div>

    <!-- Collapsible content: Generating Code & Visualizing sections -->
    <Transition name="fade">
      <div v-if="!createDataCollapsed" class="mt-2 ml-4 space-y-2">
        <!-- Generating Code section -->
        <div>
          <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click.stop="toggleCode">
            <Spinner v-if="isCodeRunning" class="w-3 h-3 mr-1.5 text-gray-400" />
            <Icon v-else-if="status === 'error'" name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-gray-400" />
            <Icon v-else-if="codeDone" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
            <span v-if="isCodeRunning && progressStage === 'validating_code'" class="tool-shimmer">Validating Code</span>
            <span v-else-if="isCodeRunning" class="tool-shimmer">Generating Code</span>
            <span v-else class="text-gray-700">Generated Code</span>
            <Icon :name="codeCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 ml-2" />
          </div>
          <Transition name="fade">
            <div v-if="!codeCollapsed && codeContent" class="mt-1 ml-4">
              <div class="bg-gray-50 rounded px-4 py-3 font-mono text-xs max-h-42 overflow-y-auto relative">
                <button
                  class="absolute top-2 right-2 px-2 py-1 text-xs rounded border border-gray-300 bg-transparent text-gray-600 hover:bg-gray-100 hover:text-gray-800"
                  :disabled="!canOpenEditor"
                  v-if="canOpenEditor"
                  @click.stop="openEditor"
                >
                  Edit code
                </button>
                <pre class="text-gray-800 whitespace-pre-wrap pr-20">{{ codeContent }}</pre>
              </div>
            </div>
          </Transition>
        </div>

        <!-- Visualizing section -->
        <div v-if="showVisualizingSection">
          <div class="flex items-center text-xs text-gray-500">
            <Spinner v-if="isVisualizing" class="w-3 h-3 mr-1.5 text-gray-400" />
            <Icon v-else-if="vizError" name="heroicons-exclamation-circle" class="w-3 h-3 mr-1.5 text-amber-500" />
            <Icon v-else-if="vizDone" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
            <span v-if="isVisualizing" class="tool-shimmer">Visualizing</span>
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
      <ToolWidgetPreview :tool-execution="toolExecution" @addWidget="onAddWidget" @toggleSplitScreen="$emit('toggleSplitScreen')" @editQuery="$emit('editQuery', $event)" />
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

const props = defineProps<Props>()
const emit = defineEmits(['addWidget', 'toggleSplitScreen', 'editQuery'])

const codeCollapsed = ref(false)
const createDataCollapsed = ref(true) // Collapsed by default
const dataTitle = computed(() => props.toolExecution.arguments_json?.title || 'Data')
const status = computed(() => props.toolExecution.status)
const progressStage = computed(() => (props.toolExecution as any).progress_stage || '')
const progressStageLabel = computed(() => {
  const s = progressStage.value
  if (!s) return ''
  const map: Record<string, string> = {
    init: 'init',
    generating_code: 'code',
    generated_code: 'code ready',
    validating_code: 'validating',
    'validating_code.retry': 'validating (retry)',
    validated_code: 'validated',
    executing_code: 'executing'
  }
  return map[s] || s
})

const codeContent = computed(() => props.toolExecution?.created_step?.code || props.toolExecution.result_json?.code || '')
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
const validationSucceeded = computed(() => {
  const stage = progressStage.value
  const valid = (props.toolExecution as any).progress_valid
  return stage === 'validated_code' && valid === true
})
const hasPreview = computed(() => {
  const te: any = props.toolExecution
  const hasObject = !!(te?.created_widget || te?.created_step)
  const hasViz = Array.isArray(te?.created_visualizations) && te.created_visualizations.length > 0
  const hasQuery = !!(te?.result_json?.query_id)
  const hasRows = Array.isArray(te?.result_json?.data?.rows) || Array.isArray(te?.result_json?.widget_data?.rows)
  return !!(hasObject || hasViz || hasQuery || hasRows)
})

const isCodeRunning = computed(() => progressStage.value && [
  'generating_code', 'generated_code', 'validating_code', 'validating_code.retry', 'executing_code'
].includes(progressStage.value))
const codeDone = computed(() => !!codeContent.value && !isCodeRunning.value)

// Visualization state
const isVisualizing = computed(() => progressStage.value === 'inferring_visualization')
const vizInferredData = computed(() => (props.toolExecution as any).progress_visualization || null)
const vizError = computed(() => (props.toolExecution as any).progress_visualization_error || null)
const vizDone = computed(() => {
  // Check if we have visualization data from progress or result
  const fromProgress = vizInferredData.value
  const fromResult = props.toolExecution.result_json as any
  return !!(fromProgress || fromResult?.data_model?.type)
})
const showVisualizingSection = computed(() => {
  // Show if visualizing, if visualization was inferred (not table), or if there's an error
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

const actionLabel = computed(() => {
  if (status.value === 'running') return `Creating data: ${dataTitle.value}`
  if (status.value === 'success') return `Created data: ${dataTitle.value}`
  if (status.value === 'error') return `Failed to create data: ${dataTitle.value}`
  return `Create data: ${dataTitle.value}`
})

const formatDuration = computed(() => {
  if (!props.toolExecution.duration_ms) return ''
  const seconds = (props.toolExecution.duration_ms / 1000).toFixed(1)
  return `${seconds}s`
})

watch([codeDone, status], ([codeNow, st]) => {
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


