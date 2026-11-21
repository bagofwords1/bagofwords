<template>
  <div class="min-w-0">
    <!-- Main Header: Creating Data (always collapsible) -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-chart-bar" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded; if (!toolHeaderExpanded) codeCollapsed = true">
      <template #label>{{ headerLabel }}</template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">


    <!-- Stopped/Error message below header -->

    <div v-if="status === 'error' && lastErrorMessage" class="mt-1 ms-4 text-chat-2 text-gray-500 dark:text-gray-400">
      {{ lastErrorMessage }}
    </div>

    <!-- Collapsible content -->
    <Transition name="fade">
      <div v-if="toolHeaderExpanded" class="mt-2 space-y-2 text-chat-3 text-ink-muted">
      <Transition name="fade-in" appear>
        <span v-if="groupedTables.length" class="inline-flex items-center flex-wrap">
          <template v-for="(group, gidx) in groupedTables" :key="gidx">
            <span v-if="gidx > 0" class="ms-1 text-gray-300 dark:text-gray-600">|</span>
            <DataSourceIcon :icon-token="group.iconToken" class="h-2.5 ms-1" :title="group.title" />
            <span v-if="group.isBow" class="ms-1 text-gray-500 dark:text-gray-400">{{ t('tools.createData.bowSource') }} ·</span>
            <span class="ms-1 text-gray-500 dark:text-gray-400 inline-flex items-center flex-wrap" :title="group.title">
              <span v-for="(nm, nidx) in group.visible" :key="nidx" class="inline-flex items-center">
                <UIcon
                  v-if="isCached(nm)"
                  name="heroicons-bolt"
                  class="w-2.5 h-2.5 me-0.5 text-amber-500 flex-shrink-0"
                  title="Cached locally — this read did not query the source"
                />
                <bdi>{{ nm }}</bdi><span v-if="nidx < group.visible.length - 1">,&nbsp;</span>
              </span>
              <UTooltip v-if="group.more" :text="group.moreTitle"><span class="text-ink-muted">&nbsp;+{{ group.more }}</span></UTooltip>
            </span>
          </template>
        </span>
      </Transition>

        <span v-if="formatDuration" class="nums ms-2">{{ formatDuration }}</span>

        <!-- Failed attempts (persisted in result_json.errors, survive refresh).
             `failedAttempts` resolves empty without `view_code` because the
             server nulls `errors` — a failed attempt's message routinely quotes
             the statement that failed, so it is withheld with the code. -->
        <div v-for="(attempt, idx) in failedAttempts" :key="'attempt-' + idx">
          <div class="flex items-center text-chat-2 text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300" @click.stop="toggleAttemptCode(idx)">
            <Icon name="heroicons-x-mark" class="w-3 h-3 me-1.5 text-amber-500" />
            <span class="text-gray-500 dark:text-gray-400">{{ t('tools.common.attempt', { n: idx + 1 }) }}</span>
            <Icon :name="attemptCodeExpanded[idx] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 ms-2 rtl-flip" />
          </div>
          <Transition name="fade">
            <div v-if="attemptCodeExpanded[idx]" class="mt-1 ms-4">
              <div v-if="attempt.code" class="bg-gray-50 dark:bg-gray-900 rounded px-3 py-2 font-mono text-chat-3 max-h-28 overflow-y-auto mb-1">
                <pre class="text-gray-600 dark:text-gray-400 whitespace-pre-wrap m-0">{{ attempt.code }}</pre>
              </div>
            </div>
          </Transition>
          <div class="mt-0.5 ms-4 text-chat-2 text-amber-600 dark:text-amber-300 bg-amber-50/50 dark:bg-amber-950 rounded px-2 py-1">
            {{ attempt.error }}
          </div>
        </div>

        <!-- Current/final code generation section. Hidden wholesale without
             `view_code`: the server sends no code, so leaving the header would
             render a row that expands to nothing. -->
        <div v-if="canViewCode">
          <button type="button" :aria-expanded="!codeCollapsed" class="flex items-center gap-1 rounded text-chat-3 text-ink-muted hover:text-ink focus:outline-none focus-visible:ring-1 focus-visible:ring-blue-500" @click.stop="codeCollapsed = !codeCollapsed">
            <Spinner v-if="isCodeGenerating && status !== 'stopped'" class="w-3 h-3 me-1.5 text-ink-muted" />
            <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 me-1.5 text-ink-muted" />
            <Icon v-if="status === 'error' && !codeContent" name="heroicons-x-mark" class="w-3 h-3 me-1.5 text-ink-muted" />
            <span v-if="isCodeGenerating && status !== 'stopped'" class="tool-shimmer">{{ t('tools.createData.generatingCode') }}</span>
            <span v-else class="text-ink-muted">{{ t('tools.createData.generatedCode') }}</span>
            <span v-if="failedAttempts.length > 0 && !isCodeGenerating" class="ms-1.5 text-ink-muted">{{ t('tools.common.attemptSuffix', { n: failedAttempts.length + 1 }) }}</span>
            <span v-if="isCodeGenerating && currentAttempt > 1" class="ms-1.5 text-ink-muted">{{ t('tools.common.attemptSuffix', { n: currentAttempt }) }}</span>
            <Icon :name="codeCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 shrink-0 rtl-flip" aria-hidden="true" />
          </button>
          <Transition name="fade">
            <div v-if="!codeCollapsed && codeContent" class="mt-1">
              <div class="bg-gray-50 dark:bg-gray-900 rounded px-4 py-3 font-mono text-chat-2 max-h-42 overflow-y-auto relative">
                <button
                  class="absolute top-2 end-2 px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 bg-transparent text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-800 dark:hover:text-gray-200"
                  :disabled="!canOpenEditor"
                  v-if="canOpenEditor && !readonly"
                  @click.stop="openEditor"
                >
                  {{ t('tools.createData.editCode') }}
                </button>
                <pre class="text-gray-800 dark:text-gray-200 whitespace-pre-wrap pe-20">{{ codeContent }}</pre>
              </div>
            </div>
          </Transition>
        </div>

        <!-- Executing Code section -->
        <div v-if="showExecutingSection">
          <div class="flex items-center text-chat-2 text-gray-500 dark:text-gray-400">
            <Spinner v-if="isExecuting && status !== 'stopped'" class="w-3 h-3 me-1.5 text-ink-muted" />
            <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 me-1.5 text-ink-muted" />
            <Icon v-else-if="executionFailed && !isRetrying" name="heroicons-x-mark" class="w-3 h-3 me-1.5 text-amber-500" />
            <span v-if="isExecuting && status !== 'stopped' && slotWait" class="tool-shimmer" data-testid="waiting-for-slot">{{ slotWaitLabel }}</span>
            <span v-else-if="isExecuting && status !== 'stopped'" class="tool-shimmer">{{ t('tools.createData.executing') }}</span>
            <span v-else-if="executionFailed" class="text-ink-muted">{{ t('tools.createData.executionFailed') }}</span>
            <span v-else class="text-ink-muted">{{ t('tools.createData.executionSucceeded') }}</span>
            <span v-if="executionDone && executionRowCount != null" class="ms-1.5 text-ink-muted">· {{ t('tools.common.rows', { n: executionRowCount }) }}</span>
            <span v-if="executionDone && formatExecutionDuration" class="ms-1.5 text-ink-muted">· {{ formatExecutionDuration }}</span>
          </div>
          <!-- Execution error from stdout (live only, before it gets captured in result_json.errors) -->
          <div v-if="latestStdoutError && !failedAttempts.length" class="mt-1 ms-4 text-chat-2 text-amber-600 dark:text-amber-300 bg-amber-50/50 dark:bg-amber-950 rounded px-2 py-1 max-h-16 overflow-y-auto">
            <pre class="whitespace-pre-wrap break-words m-0">{{ latestStdoutError }}</pre>
          </div>
        </div>

        <!-- Retry indicator (live only) -->
        <div v-if="isRetrying" class="flex items-center text-chat-2 text-gray-500 dark:text-gray-400">
          <Spinner class="w-3 h-3 me-1.5 text-ink-muted" />
          <span class="tool-shimmer">{{ t('tools.createData.retrying', { n: currentAttempt }) }}</span>
        </div>

        <!-- Visualizing section -->
        <div v-if="showVisualizingSection">
          <div class="flex items-center text-chat-2 text-gray-500 dark:text-gray-400">
            <Spinner v-if="isVisualizing && status !== 'stopped'" class="w-3 h-3 me-1.5 text-ink-muted" />
            <Icon v-else-if="status === 'stopped'" name="heroicons-stop-circle" class="w-3 h-3 me-1.5 text-ink-muted" />
            <Icon v-else-if="vizError" name="heroicons-exclamation-circle" class="w-3 h-3 me-1.5 text-amber-500" />
            <span v-if="isVisualizing && status !== 'stopped'" class="tool-shimmer">{{ t('tools.createData.visualizing') }}</span>
            <span v-else-if="vizError" class="text-ink-muted">{{ t('tools.createData.visualizing') }}</span>
            <span v-else class="text-ink-muted">{{ chartTypeLabel }}</span>
            <span v-if="vizSummary && !vizError" class="ms-1.5 text-ink-muted">· {{ vizSummary }}</span>
          </div>
          <div v-if="vizError" class="mt-1 ms-4 text-chat-2 text-gray-500 dark:text-gray-400">{{ vizError }}</div>
        </div>
      </div>
    </Transition>

    <!-- Results Preview - only show if not failed. Collapsed by default,
         charts and tables alike: the header chevron reveals it. -->
    <div class="mt-2" v-if="hasPreview && !hidePreview && status !== 'error' && toolHeaderExpanded">
      <ToolWidgetPreview :tool-execution="toolExecution" :readonly="readonly" :can-expand="canExpand" @addWidget="onAddWidget" @toggleSplitScreen="$emit('toggleSplitScreen')" @editQuery="$emit('editQuery', $event)" @openDataPanel="$emit('openDataPanel', $event)" />
    </div>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

const { t } = useResponseI18n()

import { useCachedTableNames } from '~/composables/useFastTable'
import { groupToolTables, type ToolTableGroup } from '~/composables/useToolTables'
import { computed, ref, reactive } from 'vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'
import QueryCodeEditorModal from '~/components/tools/QueryCodeEditorModal.vue'
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

// The agent shape the ticker needs: an id to match tables_by_source against and
// the backend's pre-resolved icon. Deliberately not `type`/`connections` — an
// icon derived from those here disagreed with every other view (see
// composables/useToolTables.ts).
interface DataSource {
  id: string
  icon_token?: string | null
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

// hidePreview: the host shows the result elsewhere (a folded run keeps its
// deliverables under the conclusion), so this row is the build trail only.
const props = defineProps<Props & { readonly?: boolean; dataSources?: DataSource[]; canExpand?: boolean; hidePreview?: boolean }>()
const emit = defineEmits(['addWidget', 'toggleSplitScreen', 'editQuery', 'openDataPanel'])

// Code visibility is a role permission. The server already redacts `code`,
// `errors` and the live stream's progress payloads for a user without
// `view_code`, so these gates keep the *chrome* honest too — without them a
// viewer would see an empty "Generated code" row that never opens.
const canViewCode = useCanViewCode()
const canRunCustomCode = useCanRunCustomCode()

const codeCollapsed = ref(true)
const attemptCodeExpanded = reactive<Record<number, boolean>>({})
const dataTitle = computed(() => props.toolExecution.arguments_json?.title || 'Data')
const status = computed(() => props.toolExecution.status)
const isToolRunning = computed(() => ['running', 'pending', 'queued', 'in_progress'].includes(status.value))
const progressStage = computed(() => (props.toolExecution as any).progress_stage || '')

// Code content: prefer final result, fall back to streamed progress code
const codeContent = computed(() =>
  !canViewCode.value ? '' :
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
  // A bare created_step_id is enough: ToolWidgetPreview hydrates the full
  // Step on mount when the inline copy was deduplicated to another block.
  const hasObject = !!(te?.created_widget || te?.created_step || te?.created_step_id)
  const hasViz = Array.isArray(te?.created_visualizations) && te.created_visualizations.length > 0
  const hasQuery = !!(te?.result_json?.query_id)
  const hasRows = Array.isArray(te?.result_json?.data?.rows) || Array.isArray(te?.result_json?.widget_data?.rows)
  return !!(hasObject || hasViz || hasQuery || hasRows)
})

// Stage-based state
const isCodeGenerating = computed(() => progressStage.value === 'generating_code')
const isExecuting = computed(() => progressStage.value === 'executing_code' || progressStage.value === 'waiting_for_slot')
// The connection is at its concurrent-query limit and this query waits its turn.
const slotWait = computed(() => progressStage.value === 'waiting_for_slot' ? ((props.toolExecution as any).progress_slot || {}) : null)
const slotWaitLabel = computed(() => slotWaitText(t, slotWait.value))
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
  const pastCodeGen = ['generated_code', 'executing_code', 'waiting_for_slot', 'retry', 'inferring_visualization', 'formatting_widget', 'visualization_inferred', 'series_configured'].includes(progressStage.value)
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


// Cached (BOW custom table) relations — a tool call that read one never
// touched the source database. Names come from the agent list (fetched once
// per page); the report payload doesn't carry them.
const { ensureLoaded: ensureCachedNames, isCachedTable } = useCachedTableNames()
onMounted(() => { ensureCachedNames() })
// Whether the read actually came from the cache. Two signals, and the second
// is the one that holds up.
//
// `tables_by_source` is what the PLANNER said it would use, and it can name a
// table that does not exist — in which case the name never matches a cached
// relation and the badge silently doesn't render, even though the code that
// ran read from the cache. The executed code is not a claim: a query issued
// through a `…::fast` client provably did not touch the source.
const executedOnCache = computed(() => {
  const code = String(codeContent.value || '')
  return /ds_clients\[\s*["'][^"']*::fast["']\s*\]/.test(code)
})
const isCached = (n: string) => isCachedTable(n) || executedOnCache.value

// Prefer the agents the backend attached to THIS tool execution: that list rides
// along in every view (report page, shared /c/{token}, trace modal), whereas the
// `dataSources` prop is only passed on the report page — which is why the shared
// views used to fall back to a generic icon. The prop stays as a fallback.
const iconAgents = computed<DataSource[]>(
  () => (props.toolExecution as any)?.data_sources || props.dataSources || [],
)

const groupedTables = computed<ToolTableGroup[]>(() =>
  groupToolTables((props.toolExecution as any)?.arguments_json, iconAgents.value)
)

// The header names what the call makes: the model's title from the moment
// its arguments arrive, the stored step's title once it exists.
const headerTitle = computed(() => String(
  (props.toolExecution as any)?.created_step?.title || props.toolExecution.arguments_json?.title || '',
).trim())

const headerLabel = computed(() => {
  const title = headerTitle.value
  if (status.value === 'success') return title ? t('tools.createData.createdTitled', { title }) : t('tools.createData.created')
  if (status.value === 'error' || status.value === 'failed') return title ? t('reportView.runFold.steps.dataFailed', { title }) : t('tools.createData.create')
  return title ? t('tools.createData.creatingTitled', { title }) : t('tools.createData.creating')
})

function toggleAttemptCode(idx: number) { attemptCodeExpanded[idx] = !attemptCodeExpanded[idx] }
function onAddWidget(payload: { widget?: any, step?: any }) { emit('addWidget', payload) }

const initialStepId = computed(() => props.toolExecution?.created_step_id || props.toolExecution?.created_step?.id || null)
const createdQueryId = computed(() => {
  const stepQ = (props.toolExecution?.created_step as any)?.query_id
  if (stepQ) return stepQ
  const resultQ = (props.toolExecution as any)?.result_json?.query_id
  return resultQ || null
})
const canOpenEditor = computed(() =>
  canRunCustomCode.value
  && !!(initialStepId.value || createdQueryId.value || codeContent.value))
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
