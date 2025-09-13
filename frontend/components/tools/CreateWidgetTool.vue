<template>
  <div class="mb-2">
    <div class="flex items-center text-xs text-gray-500 hidden">

      <!-- Status icon -->
      <Icon v-if="status === 'success'" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-red-500" />

      <!-- Action label with shimmer for running -->
      <span v-if="status === 'running'" class="tool-shimmer">
        {{ actionLabel }}
      </span>
      <span v-else class="text-gray-700">{{ actionLabel }}</span>

      <!-- Stage badge -->
      <span v-if="progressStage" class="ml-2 px-1.5 py-0.5 rounded bg-gray-100 text-gray-400">{{ progressStageLabel }}</span>

      <!-- Execution time if > 2 seconds -->
      <span v-if="showDuration" class="ml-2 text-gray-400">{{ formatDuration }}</span>
    </div>

    <!-- Collapsible content -->
    <Transition name="fade">
      <div class="mt-3">
        <!-- Section 1: Creating Data Model -->
        <div class="mb-4">
          <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleDm">
            <Icon v-if="isDMRunning" name="eos-icons:loading" class="w-3 h-3 mr-1.5 animate-spin text-gray-400" />
            <Icon v-else-if="dmDone" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
            <span v-if="isDMRunning" class="tool-shimmer">Creating Data Model</span>
            <span v-else class="text-gray-700">Creating Data Model</span>
            <Icon :name="dmCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 ml-2" />
          </div>
          <Transition name="fade">
            <div v-if="!dmCollapsed" class="mt-1 ml-4">
              <div v-if="dataModelColumns.length > 0" class="text-xs mt-2">
                <table class="w-full text-xs mt-2">
                  <tbody>
                    <tr v-for="column in dataModelColumns" :key="column.generated_column_name" class="border-b border-gray-100">
                      <td class="font-mono text-gray-800 py-1 pr-4 align-top">
                        {{ column.generated_column_name }}
                      </td>
                      <td class="text-gray-500 py-1 leading-tight">
                        {{ column.description }}
                        <span v-if="column.source" class="text-gray-400 text-xs">
                          <Icon name="heroicons-circle-stack" class="w-3 h-3 ml-1 text-gray-400" />
                          {{ column.source }}
                        </span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-else class="text-xs text-gray-400 mt-1 hidden">Preparing…</div>
            </div>
          </Transition>
        </div>

        <!-- Section 2: Generating Code (only show after data model is completed) -->
        <div class="mb-2" v-if="dmDone">
          <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCode">
            <Icon v-if="isCodeRunning" name="eos-icons:loading" class="w-3 h-3 mr-1.5 animate-spin text-gray-400" />
            <Icon v-else-if="status === 'error'" name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-red-500" />
            <Icon v-else-if="codeDone" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
            <span v-if="isCodeRunning && progressStage === 'validating_code'" class="tool-shimmer">Validating Code</span>
            <span v-else-if="isCodeRunning" class="tool-shimmer">Generating Code</span>
            <span v-else class="text-gray-700">Generating Code</span>
            <Icon :name="codeCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 ml-2" />
          </div>
          <Transition name="fade">
            <div v-if="!codeCollapsed" class="mt-1 ml-4">
              <div v-if="codeContent" class="text-xs mb-2 mt-1">

                
                
                <div class="mb-2 text-xs bg-gray-50 rounded-lg px-4 py-3 text-gray-500 flex items-center">
                  <span v-if="isCodeRunning && progressStage === 'validating_code'" class="tool-shimmer">Validating... (Attempt {{ currentAttempt }})</span>
                  <span v-else-if="isCodeRunning" class="tool-shimmer">Running... (Attempt {{ currentAttempt }})</span>
                  <span v-else-if="status === 'success'" class="flex items-center">
                    <span class="text-green-500 flex items-center">
                      <Icon name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
                      {{ validationSucceeded ? 'Success and validated' : 'Success' }}</span>
                    <span class="ml-2" v-if="successDetails"> • {{ successDetails }}</span>
                  </span>
                  <span v-else-if="status === 'error'" class="flex items-center">
                    <span class="text-red-500 flex items-center">
                      <Icon name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-red-500" />
                      Failed</span>
                    <span class="ml-2 text-red-500" v-if="lastErrorMessage"> • {{ lastErrorMessage }}</span>
                  </span>
                  <div class="flex-1"></div>
                  <!-- Right aligned attempts with hover popover listing errors -->
                  <div class="relative group">
                    <span class="text-gray-400 cursor-default">attempts: {{ currentAttempt }}</span>
                    <div class="hidden group-hover:block absolute right-0 z-10 mt-1 w-80 bg-white border border-gray-200 rounded shadow-lg p-2 text-xs text-gray-600">
                      <div v-if="attempts && attempts.length">
                        <div class="font-medium text-gray-700 mb-1">Errors</div>
                        <ul class="list-disc ml-5 max-h-48 overflow-auto">
                          <li v-for="(att, idx) in attempts" :key="idx">Attempt {{ idx + 1 }}: {{ att }}</li>
                        </ul>
                      </div>
                      <div v-else class="text-gray-400"></div>
                    </div>
                  </div>
                </div>
                <div class="bg-gray-50 rounded px-4 py-3 font-mono text-xs max-h-42 overflow-y-auto">
                  <pre class="text-gray-800 whitespace-pre-wrap">{{ codeContent }}</pre>
                </div>
                <div class="mt-2">
                  <button
                    class="px-3 py-1.5 text-xs rounded bg-gray-800 text-white hover:bg-gray-700"
                    :disabled="!canOpenEditor"
                    @click.stop="openEditor"
                  >
                    Edit code
                  </button>
                </div>

              </div>
              <div v-else class="text-xs text-gray-400 mt-1 hidden">Preparing…</div>
            </div>
          </Transition>
        </div>

        <!-- Result summary fallback (only when no sections have content) -->
        <div v-if="!dataModelColumns.length && !codeContent && resultSummary" class="text-xs text-gray-600">
          {{ resultSummary }}
        </div>

        <!-- Error message intentionally suppressed under the toggle to keep UI clean -->

        <!-- Results (shown only on success) -->
        <div class="mt-1" v-if="hasPreview">
          <ToolWidgetPreview :tool-execution="toolExecution" @addWidget="onAddWidget" @toggleSplitScreen="$emit('toggleSplitScreen')" @editQuery="$emit('editQuery', $event)" />
        </div>

        <!-- Final status summary -->
        <div class="mt-2 text-xs" v-if="status !== 'running'">
        </div>
      </div>
    </Transition>
  </div>
  <QueryCodeEditorModal
    :visible="showEditor"
    :query-id="createdQueryId"
    :initial-code="codeContent || ''"
    :title="widgetTitle"
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

interface Props {
  toolExecution: {
    id: string
    tool_name: string
    tool_action?: string
    arguments_json?: {
      widget_title?: string
      user_prompt?: string
      interpreted_prompt?: string
    }
    result_json?: {
      data_model?: {
        type?: string
        columns?: Array<{
          generated_column_name: string
          description: string
          source?: string
        }>
      }
      code?: string
      widget_data?: any
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

const emit = defineEmits(['addWidget', 'refreshDashboard', 'toggleSplitScreen', 'editQuery'])
// Per-section collapsed state
const dmCollapsed = ref(false)
const codeCollapsed = ref(false)
const resultsCollapsed = ref(false)

const widgetTitle = computed(() => props.toolExecution.arguments_json?.widget_title || 'Widget')
const status = computed(() => props.toolExecution.status)
const resultSummary = computed(() => props.toolExecution.result_summary)
const progressStage = computed(() => (props.toolExecution as any).progress_stage || '')
const progressStageLabel = computed(() => {
  const s = progressStage.value
  if (!s) return ''
  const map: Record<string, string> = {
    init: 'init',
    generating_data_model: 'data model',
    data_model_type_determined: 'model type',
    column_added: 'column',
    series_configured: 'series',
    widget_creation_needed: 'finalizing model',
    generating_code: 'code',
    generated_code: 'code ready',
    validating_code: 'validating',
    'validating_code.retry': 'validating (retry)',
    validated_code: 'validated',
    executing_code: 'executing'
  }
  return map[s] || s
})

const dataModelType = computed(() => props.toolExecution.result_json?.data_model?.type || null)
const dataModelColumns = computed(() => props.toolExecution.result_json?.data_model?.columns || [])

const codeContent = computed(() => props.toolExecution?.created_step?.code || props.toolExecution.result_json?.code || '')
const successDetails = computed(() => {
  if (status.value !== 'success') return null
  const totalRows = props.toolExecution.result_json?.stats?.total_rows || props.toolExecution.result_json?.widget_data?.info?.total_rows
  return totalRows !== undefined ? `${totalRows.toLocaleString()} rows` : null
})

// Error attempts and last error
const attempts = computed(() => {
  const errs = (props.toolExecution.result_json as any)?.errors || []
  return errs.map((pair: any) => {
    const msg = Array.isArray(pair) ? pair[1] : (pair?.message || String(pair))
    const firstLine = (msg || '').split('\n')[0]
    return firstLine
  })
})
const lastErrorMessage = computed(() => attempts.value?.[attempts.value.length - 1] || '')

// Current attempt while running; fallback to attempts length + 1
const currentAttempt = computed(() => {
  const pa = (props.toolExecution as any).progress_attempt
  if (typeof pa === 'number') return pa + 1
  const len = attempts.value?.length || 0
  return len > 0 ? len + 1 : 1
})

// Validation success flag when progress reports validated_code with valid true
const validationSucceeded = computed(() => {
  const stage = progressStage.value
  const valid = (props.toolExecution as any).progress_valid
  return stage === 'validated_code' && valid === true
})

const hasPreview = computed(() => !!(props.toolExecution?.created_widget || props.toolExecution?.created_step))

// Running/done flags based on progress stage and status
const isDMRunning = computed(() => progressStage.value && [
  'generating_data_model', 'data_model_type_determined', 'column_added', 'series_configured', 'widget_creation_needed'
].includes(progressStage.value))
const dmDone = computed(() => !!dataModelType.value && dataModelColumns.value.length >= 0 && !isDMRunning.value)

const isCodeRunning = computed(() => progressStage.value && [
  'generating_code', 'generated_code', 'validating_code', 'validating_code.retry', 'executing_code'
].includes(progressStage.value))
const codeDone = computed(() => !!codeContent.value && !isCodeRunning.value)

const actionLabel = computed(() => {
  if (status.value === 'running') return `Creating widget: ${widgetTitle.value}`
  if (status.value === 'success') return `Created widget: ${widgetTitle.value}`
  if (status.value === 'error') return `Failed to create widget: ${widgetTitle.value}`
  return `Create widget: ${widgetTitle.value}`
})

const showDuration = computed(() => props.toolExecution.duration_ms && props.toolExecution.duration_ms > 2000)
const formatDuration = computed(() => {
  if (!props.toolExecution.duration_ms) return ''
  const seconds = (props.toolExecution.duration_ms / 1000).toFixed(1)
  return `${seconds}s`
})

function toggleCollapsed() {
  isCollapsed.value = !isCollapsed.value
}

// Collapse DM and Code by default if each is completed
watch([dmDone, codeDone, status], ([dmNow, codeNow, st]) => {
  if (dmNow) dmCollapsed.value = true
  // Never auto-collapse the code section when there is an error
  if (st === 'error') {
    codeCollapsed.value = false
  } else if (codeNow) {
    codeCollapsed.value = true
  }
}, { immediate: true })

function toggleDm() { dmCollapsed.value = !dmCollapsed.value }
function toggleCode() { codeCollapsed.value = !codeCollapsed.value }
function toggleResults() { resultsCollapsed.value = !resultsCollapsed.value }

function onAddWidget(payload: { widget?: any, step?: any }) {
  emit('addWidget', payload)
}

const createdWidgetId = computed(() => props.toolExecution?.created_widget_id || props.toolExecution?.created_widget?.id || null)
const initialStepId = computed(() => props.toolExecution?.created_step_id || props.toolExecution?.created_step?.id || null)

// Prefer query from created_step if available; otherwise, from created_widget
const createdQueryId = computed(() => {
  const visList = (props.toolExecution as any)?.created_visualizations
  if (Array.isArray(visList) && visList.length && visList[0]?.query_id) {
    return visList[0].query_id
  }
  const stepQ = (props.toolExecution?.created_step as any)?.query_id
  if (stepQ) return stepQ
  const widgetQ = (props.toolExecution?.created_widget as any)?.query_id
  if (widgetQ) return widgetQ
  const resultQ = (props.toolExecution as any)?.result_json?.query_id
  return resultQ || null
})

const canOpenEditor = computed(() => !!(initialStepId.value || createdQueryId.value))

async function openEditor() {
  if (!initialStepId.value) return
  showEditor.value = true
}

const showEditor = ref(false)

function onModalSaved(step: any) {
  // update toolExecution with the new step id so next open shows latest
  (props.toolExecution as any).created_step_id = step?.id
  ;(props.toolExecution as any).created_step = step
  emit('addWidget', { step })
  // Ask parent page to refresh dashboard queries/layout so the latest step is shown
  emit('refreshDashboard')
  // Broadcast default step change to dashboard listeners for real-time tile updates
  try {
    const qid = createdQueryId.value
    if (qid) {
      window.dispatchEvent(new CustomEvent('query:default_step_changed', { detail: { query_id: qid, step } }))
    }
  } catch {}
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

@keyframes shimmer {
  0% { background-position: -100% 0; }
  100% { background-position: 100% 0; }
}

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


