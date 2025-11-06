<template>
  <div class="mb-2">
    <div class="flex items-center text-xs text-gray-500 hidden">
      <Icon v-if="status === 'success'" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-red-500" />
      <span v-if="status === 'running'" class="tool-shimmer">{{ actionLabel }}</span>
      <span v-else class="text-gray-700">{{ actionLabel }}</span>
      <span v-if="progressStage" class="ml-2 px-1.5 py-0.5 rounded bg-gray-100 text-gray-400">{{ progressStageLabel }}</span>
      <span v-if="showDuration" class="ml-2 text-gray-400">{{ formatDuration }}</span>
    </div>

    <Transition name="fade">
      <div class="mt-3">
        <!-- Section: Generating Code -->
        <div class="mb-2">
          <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCode">
            <Spinner v-if="isCodeRunning" class="w-3 h-3 mr-1.5 text-gray-400" />
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
              <div v-else class="text-xs text-gray-400 mt-1 hidden">Preparing…</div>
            </div>
          </Transition>
        </div>

        <!-- Results Preview -->
        <div class="mt-1" v-if="hasPreview">
          <ToolWidgetPreview :tool-execution="toolExecution" @addWidget="onAddWidget" @toggleSplitScreen="$emit('toggleSplitScreen')" @editQuery="$emit('editQuery', $event)" />
        </div>
      </div>
    </Transition>
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

const actionLabel = computed(() => {
  if (status.value === 'running') return `Creating data: ${dataTitle.value}`
  if (status.value === 'success') return `Created data: ${dataTitle.value}`
  if (status.value === 'error') return `Failed to create data: ${dataTitle.value}`
  return `Create data: ${dataTitle.value}`
})

const showDuration = computed(() => props.toolExecution.duration_ms && props.toolExecution.duration_ms > 2000)
const formatDuration = computed(() => {
  if (!props.toolExecution.duration_ms) return ''
  const seconds = (props.toolExecution.duration_ms / 1000).toFixed(1)
  return `${seconds}s`
})

watch([codeDone, status], ([codeNow, st]) => {
  if (st === 'error') {
    codeCollapsed.value = false
  } else if (codeNow) {
    codeCollapsed.value = true
  }
}, { immediate: true })

function toggleCode() { codeCollapsed.value = !codeCollapsed.value }
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


