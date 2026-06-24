<template>
  <div class="mb-2">
    <!-- Main Header: Creating Data (always collapsible) -->
    <div class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" @click="toggleCreateData">
      <Icon :name="createDataCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 me-1.5 text-gray-400 rtl-flip" />
      <Spinner v-if="status === 'running'" class="w-3 h-3 me-1.5 text-gray-400" />
      <Icon v-else-if="status === 'success'" name="heroicons-check" class="w-3 h-3 me-1.5 text-green-500" />
      <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 me-1.5 text-gray-400" />
      <Icon v-else-if="status === 'error'" name="heroicons-exclamation-circle" class="w-3 h-3 me-1.5 text-amber-500" />
      <span v-if="status === 'running'" class="tool-shimmer">{{ $t('tools.createData.creating') }}</span>
      <span v-else-if="status === 'success'" class="text-gray-700 dark:text-gray-300">{{ $t('tools.createData.created') }}</span>
      <span v-else-if="status === 'stopped'" class="text-gray-700 dark:text-gray-300 italic">{{ $t('tools.createData.creating') }}</span>
      <span v-else-if="status === 'error'" class="text-gray-700 dark:text-gray-300">{{ $t('tools.createData.create') }}</span>
      <span v-else class="text-gray-700 dark:text-gray-300">{{ $t('tools.createData.create') }}</span>
      <Transition name="fade-in" appear>
        <span v-if="groupedTables.length" class="inline-flex items-center flex-wrap">
          <template v-for="(group, gidx) in groupedTables" :key="gidx">
            <span v-if="gidx > 0" class="ms-1 text-gray-300 dark:text-gray-600">|</span>
            <DataSourceIcon :type="group.type" class="h-2.5 ms-1" :title="group.names.join(', ')" />
            <span class="ms-1 text-gray-500 dark:text-gray-400">{{ group.names.join(', ') }}</span>
          </template>
        </span>
      </Transition>
      <span v-if="status === 'running' && liveStageKey" class="ms-1.5 text-gray-400">
        · <span class="tool-shimmer">{{ $t(`tools.createData.${liveStageKey}`) }}</span>
      </span>
      <span v-if="formatDuration" class="ms-1.5 text-gray-400">{{ formatDuration }}</span>
    </div>

    <!-- Stopped/Error message below header -->
    <div v-if="status === 'stopped'" class="mt-1 ms-4 text-xs text-gray-400 italic">{{ $t('tools.common.generationStopped') }}</div>
    <div v-else-if="status === 'error' && lastErrorMessage" class="mt-1 ms-4 text-xs text-gray-500 dark:text-gray-400">
      {{ lastErrorMessage }}
    </div>

    <!-- Collapsible content -->
    <Transition name="fade">
      <div v-if="!createDataCollapsed" class="mt-2 ms-4 space-y-2">

        <!-- Failed attempts (persisted in result_json.errors, survive refresh) -->
        <div v-for="(attempt, idx) in failedAttempts" :key="'attempt-' + idx">
          <div class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" @click.stop="toggleAttemptCode(idx)">
            <Icon name="heroicons-x-mark" class="w-3 h-3 me-1.5 text-amber-500" />
            <span class="text-gray-500 dark:text-gray-400">{{ $t('tools.common.attempt', { n: idx + 1 }) }}</span>
            <Icon :name="attemptCodeExpanded[idx] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 ms-2 rtl-flip" />
          </div>
          <Transition name="fade">
            <div v-if="attemptCodeExpanded[idx]" class="mt-1 ms-4">
              <div v-if="attempt.code" class="bg-gray-50 dark:bg-gray-900 rounded px-3 py-2 font-mono text-[10px] max-h-28 overflow-y-auto mb-1">
                <pre class="text-gray-600 dark:text-gray-400 whitespace-pre-wrap m-0">{{ attempt.code }}</pre>
              </div>
            </div>
          </Transition>
          <div class="mt-0.5 ms-4 text-[11px] text-amber-600 bg-amber-50/50 dark:bg-amber-950 rounded px-2 py-1">
            {{ attempt.error }}
          </div>
        </div>

        <!-- Current/final code generation section -->
        <div>
          <div class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" @click.stop="toggleCode">
            <Spinner v-if="isCodeGenerating && status !== 'stopped'" class="w-3 h-3 me-1.5 text-gray-400" />
            <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 me-1.5 text-gray-400" />
            <Icon v-else-if="status === 'error' && !codeContent" name="heroicons-x-mark" class="w-3 h-3 me-1.5 text-gray-400" />
            <Icon v-else-if="codeGenDone" name="heroicons-check" class="w-3 h-3 me-1.5 text-green-500" />
            <Icon v-else name="heroicons-minus" class="w-3 h-3 me-1.5 text-gray-300 dark:text-gray-600" />
            <span v-if="isCodeGenerating && status !== 'stopped'" class="tool-shimmer">{{ $t('tools.createData.generatingCode') }}</span>
            <span v-else class="text-gray-700 dark:text-gray-300">{{ $t('tools.createData.generatedCode') }}</span>
            <span v-if="failedAttempts.length > 0 && !isCodeGenerating" class="ms-1.5 text-gray-400">{{ $t('tools.common.attemptSuffix', { n: failedAttempts.length + 1 }) }}</span>
            <span v-if="isCodeGenerating && currentAttempt > 1" class="ms-1.5 text-gray-400">{{ $t('tools.common.attemptSuffix', { n: currentAttempt }) }}</span>
            <Icon :name="codeCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 ms-2 rtl-flip" />
          </div>
          <Transition name="fade">
            <div v-if="!codeCollapsed && codeContent" class="mt-1 ms-4">
              <div class="bg-gray-50 dark:bg-gray-900 rounded px-4 py-3 font-mono text-xs max-h-42 overflow-y-auto relative">
                <button
                  class="absolute top-2 end-2 px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 bg-transparent text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-800 dark:hover:text-gray-200"
                  :disabled="!canOpenEditor"
                  v-if="canOpenEditor && !readonly"
                  @click.stop="openEditor"
                >
                  {{ $t('tools.createData.editCode') }}
                </button>
                <pre class="text-gray-800 dark:text-gray-200 whitespace-pre-wrap pe-20">{{ codeContent }}</pre>
              </div>
            </div>
          </Transition>
        </div>

        <!-- Executing Code section -->
        <div v-if="showExecutingSection">
          <div class="flex items-center text-xs text-gray-500 dark:text-gray-400">
            <Spinner v-if="isExecuting && status !== 'stopped'" class="w-3 h-3 me-1.5 text-gray-400" />
            <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 me-1.5 text-gray-400" />
            <Icon v-else-if="executionFailed && !isRetrying" name="heroicons-x-mark" class="w-3 h-3 me-1.5 text-amber-500" />
            <Icon v-else-if="executionDone" name="heroicons-check" class="w-3 h-3 me-1.5 text-green-500" />
            <Icon v-else name="heroicons-minus" class="w-3 h-3 me-1.5 text-gray-300 dark:text-gray-600" />
            <span v-if="isExecuting && status !== 'stopped'" class="tool-shimmer">{{ $t('tools.createData.executing') }}</span>
            <span v-else-if="executionFailed" class="text-gray-700 dark:text-gray-300">{{ $t('tools.createData.executionFailed') }}</span>
            <span v-else class="text-gray-700 dark:text-gray-300">{{ $t('tools.createData.executionSucceeded') }}</span>
            <span v-if="executionDone && executionRowCount != null" class="ms-1.5 text-gray-400">· {{ $t('tools.common.rows', { n: executionRowCount }) }}</span>
            <span v-if="executionDone && formatExecutionDuration" class="ms-1.5 text-gray-400">· {{ formatExecutionDuration }}</span>
          </div>
          <!-- Execution error from stdout (live only, before it gets captured in result_json.errors) -->
          <div v-if="latestStdoutError && !failedAttempts.length" class="mt-1 ms-4 text-[11px] text-amber-600 bg-amber-50/50 dark:bg-amber-950 rounded px-2 py-1 max-h-16 overflow-y-auto">
            <pre class="whitespace-pre-wrap break-words m-0">{{ latestStdoutError }}</pre>
          </div>
        </div>

        <!-- Retry indicator (live only) -->
        <div v-if="isRetrying" class="flex items-center text-xs text-gray-500 dark:text-gray-400">
          <Spinner class="w-3 h-3 me-1.5 text-gray-400" />
          <span class="tool-shimmer">{{ $t('tools.createData.retrying', { n: currentAttempt }) }}</span>
        </div>

        <!-- Visualizing section -->
        <div v-if="showVisualizingSection">
          <div class="flex items-center text-xs text-gray-500 dark:text-gray-400">
            <Spinner v-if="isVisualizing && status !== 'stopped'" class="w-3 h-3 me-1.5 text-gray-400" />
            <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 me-1.5 text-gray-400" />
            <Icon v-else-if="vizError" name="heroicons-exclamation-circle" class="w-3 h-3 me-1.5 text-amber-500" />
            <Icon v-else-if="vizDone" name="heroicons-check" class="w-3 h-3 me-1.5 text-green-500" />
            <span v-if="isVisualizing && status !== 'stopped'" class="tool-shimmer">{{ $t('tools.createData.visualizing') }}</span>
            <span v-else-if="vizError" class="text-gray-700 dark:text-gray-300">{{ $t('tools.createData.visualizing') }}</span>
            <span v-else class="text-gray-700 dark:text-gray-300">{{ chartTypeLabel }}</span>
            <span v-if="vizSummary && !vizError" class="ms-1.5 text-gray-400">· {{ vizSummary }}</span>
          </div>
          <div v-if="vizError" class="mt-1 ms-4 text-xs text-gray-500 dark:text-gray-400">{{ vizError }}</div>
        </div>
      </div>
    </Transition>

    <!-- Results Preview - only show if not failed.
         Small table results stay hidden until the user expands the tool via
         the existing header chevron; charts and larger tables always render. -->
    <div class="mt-2" v-if="hasPreview && status !== 'error' && (!autoCollapsePreview || !createDataCollapsed)">
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
import { computed, ref, reactive } from 'vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'
import QueryCodeEditorModal from '~/components/tools/QueryCodeEditorModal.vue'
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface DataSource {
  id: string
  type?: string
  data_source_type?: string
  connections?: Array<{ id: string; type: string }>
}

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

const props = defineProps<Props & { readonly?: boolean; dataSources?: DataSource[] }>()
const emit = defineEmits(['addWidget', 'toggleSplitScreen', 'editQuery'])

const codeCollapsed = ref(true)
const createDataCollapsed = ref(true) // Collapsed by default
const attemptCodeExpanded = reactive<Record<number, boolean>>({})
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

// Failed attempts from result_json.errors: [[code, error], ...]
// This data survives refresh since it's persisted in tool execution result_json
const failedAttempts = computed(() => {
  const errs = (props.toolExecution.result_json as any)?.errors || []
  return errs.map((pair: any) => {
    const code = Array.isArray(pair) ? pair[0] : (pair?.code || '')
    const rawError = Array.isArray(pair) ? pair[1] : (pair?.message || String(pair))
    // Show first line of error for compact display
    const error = (rawError || '').split('\n')[0]
    return { code, error }
  })
})

const lastErrorMessage = computed(() => {
  const last = failedAttempts.value[failedAttempts.value.length - 1]
  return last?.error || ''
})
const currentAttempt = computed(() => {
  const pa = (props.toolExecution as any).progress_attempt
  if (typeof pa === 'number') return pa + 1
  const len = failedAttempts.value?.length || 0
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

// Compact i18n key for the live progress stage shown inline in the header.
// Maps backend progress_stage to one of three coarse labels.
const liveStageKey = computed<string>(() => {
  const stage = progressStage.value
  if (!stage) return ''
  if (stage === 'generating_code') return 'generatingCode'
  if (stage === 'executing_code' || stage === 'retry') return 'executing'
  if (['formatting_widget', 'inferring_visualization', 'visualization_inferred', 'series_configured'].includes(stage)) {
    return 'visualizing'
  }
  return ''
})

// Stage-based state
const isCodeGenerating = computed(() => progressStage.value === 'generating_code')
const codeGenDone = computed(() => !!codeContent.value && !isCodeGenerating.value)
const isExecuting = computed(() => progressStage.value === 'executing_code')
const isRetrying = computed(() => progressStage.value === 'retry')
const executionDone = computed(() => {
  if (status.value === 'success') return true
  const pastExecution = ['inferring_visualization', 'formatting_widget', 'visualization_inferred', 'series_configured'].includes(progressStage.value)
  return pastExecution
})
const executionFailed = computed(() => {
  if (status.value === 'error') return true
  return false
})

// Show executing section once code generation is done
const showExecutingSection = computed(() => {
  if (status.value === 'stopped') return false
  if (isRetrying.value) return false
  // After refresh: show if we have code (success or error with code)
  if (status.value !== 'running' && codeContent.value) return true
  // During streaming: show once past code generation
  const pastCodeGen = ['generated_code', 'executing_code', 'retry', 'inferring_visualization', 'formatting_widget', 'visualization_inferred', 'series_configured'].includes(progressStage.value)
  return pastCodeGen || isExecuting.value || executionDone.value
})

// Stdout errors from progress_stdout (live streaming only)
const stdoutMessages = computed(() => (props.toolExecution as any).progress_stdout || [])
const latestStdoutError = computed(() => {
  if (!stdoutMessages.value.length) return ''
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

const vizSummary = computed(() => {
  const series = vizInferredData.value?.series || (props.toolExecution.result_json as any)?.data_model?.series || []
  if (!series.length) return ''
  const s = series[0]
  if (s.x && s.y) return `${s.x} → ${s.y}`
  if (s.key && s.value) return `${s.key} → ${s.value}`
  if (s.value) return s.value
  return ''
})

const executionRowCount = computed(() => {
  const stats = props.toolExecution.result_json?.stats
  if (stats?.total_rows != null) return stats.total_rows
  const rows = (props.toolExecution.result_json as any)?.data?.rows
  if (Array.isArray(rows)) return rows.length
  return null
})

const formatExecutionDuration = computed(() => {
  const ms = (props.toolExecution.result_json as any)?.execution_ms
  if (!ms) return ''
  const seconds = (ms / 1000).toFixed(1)
  return `${seconds}s`
})

const formatDuration = computed(() => {
  if (!props.toolExecution.duration_ms) return ''
  const seconds = (props.toolExecution.duration_ms / 1000).toFixed(1)
  return `${seconds}s`
})


const groupedTables = computed<Array<{ type: string; names: string[] }>>(() => {
  const aj = (props.toolExecution as any)?.arguments_json || {}
  if (!Array.isArray(aj.tables_by_source)) return []
  const groups: Record<string, string[]> = {}
  for (const group of aj.tables_by_source) {
    let connType = 'resource'
    if (group.data_source_id && props.dataSources?.length) {
      const ds = props.dataSources.find((d) => d.id === group.data_source_id)
      if (ds) {
        connType = ds.connections?.[0]?.type || ds.type || ds.data_source_type || 'resource'
      }
    }
    if (!groups[connType]) groups[connType] = []
    if (Array.isArray(group.tables)) {
      groups[connType].push(...group.tables)
    }
  }
  return Object.entries(groups).map(([type, names]) => ({ type, names }))
})

// Hide the data preview by default for small table results (< 10 rows).
// The agent describes these in its text answer, so the inline table is
// redundant — the existing header chevron (createDataCollapsed) reveals it
// when wanted. Charts and metric cards are visualizations and always render.
const SMALL_RESULT_THRESHOLD = 10
const resultDataModelType = computed(() => (props.toolExecution.result_json as any)?.data_model?.type || '')
const isTableResult = computed(() => !resultDataModelType.value || resultDataModelType.value === 'table')
const autoCollapsePreview = computed(() =>
  isTableResult.value
  && executionRowCount.value != null
  && executionRowCount.value < SMALL_RESULT_THRESHOLD
)

function toggleCode() { codeCollapsed.value = !codeCollapsed.value }
function toggleCreateData() { createDataCollapsed.value = !createDataCollapsed.value }
function toggleAttemptCode(idx: number) { attemptCodeExpanded[idx] = !attemptCodeExpanded[idx] }
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
