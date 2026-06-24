<template>
  <div class="mt-1">
    <!-- Header line -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center gap-1.5">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
          <span>{{ $t('tools.listAgentExecutions.loading') }}</span>
        </span>
        <span v-else-if="isSuccess" class="flex items-center gap-1.5 text-gray-600 dark:text-gray-400">
          <Icon name="heroicons-list-bullet" class="w-3 h-3 text-blue-500" />
          <span class="font-medium">{{ $t('tools.listAgentExecutions.title') }}</span>
          <span class="px-1.5 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 rounded text-[10px]">{{ executions.length }}/{{ output.total }}</span>
          <span v-if="appliedFilter" class="px-1.5 py-0.5 bg-blue-50 dark:bg-blue-950 text-blue-600 rounded text-[10px]">{{ filterLabel(appliedFilter) }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 text-gray-400 dark:text-gray-500 rtl-flip"
          />
        </span>
        <span v-else class="flex items-center gap-1.5 text-gray-600 dark:text-gray-400">
          <Icon name="heroicons-x-circle" class="w-3 h-3 text-red-500" />
          <span>{{ $t('tools.listAgentExecutions.failed') }}</span>
        </span>
      </div>
    </Transition>

    <!-- Expanded content -->
    <Transition name="slide">
      <div v-if="isExpanded && isSuccess && executions.length > 0" class="mt-2">
        <!-- Execution rows -->
        <div class="space-y-px">
          <div
            v-for="item in pagedExecutions"
            :key="item.agent_execution_id"
            class="flex items-center gap-2 px-2 py-1.5 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors min-w-0"
            @click.stop="openTrace(item)"
          >
            <!-- Status dot -->
            <span
              class="w-1.5 h-1.5 rounded-full flex-shrink-0"
              :class="item.status === 'error' ? 'bg-red-400' : 'bg-green-400'"
            />
            <!-- Prompt (short, fixed width) -->
            <span class="text-[11px] text-gray-700 dark:text-gray-300 truncate flex-shrink-0 w-32">
              {{ item.prompt || '—' }}
            </span>
            <!-- Step title chips or tool name pills (fills remaining space) -->
            <span class="flex items-center gap-1 flex-1 min-w-0 overflow-hidden">
              <template v-if="item.step_titles && item.step_titles.length > 0">
                <span
                  v-for="(title, idx) in item.step_titles.slice(0, 2)"
                  :key="'s'+idx"
                  class="px-1.5 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 rounded text-[9px] leading-none truncate max-w-[120px] flex-shrink-0"
                >{{ title }}</span>
                <span v-if="item.step_titles.length > 2" class="text-[9px] text-gray-400 dark:text-gray-500 flex-shrink-0">+{{ item.step_titles.length - 2 }}</span>
              </template>
              <template v-else-if="item.tool_names && item.tool_names.length > 0">
                <span
                  v-for="(name, idx) in item.tool_names.slice(0, 3)"
                  :key="'t'+idx"
                  class="px-1.5 py-0.5 bg-blue-50 dark:bg-blue-950 text-blue-600 rounded text-[9px] leading-none truncate max-w-[110px] flex-shrink-0"
                >{{ toolLabel(name) }}</span>
                <span v-if="item.tool_names.length > 3" class="text-[9px] text-gray-400 dark:text-gray-500 flex-shrink-0">+{{ item.tool_names.length - 3 }}</span>
              </template>
              <span v-else class="text-[9px] text-gray-300 dark:text-gray-600 italic">no output</span>
            </span>
            <!-- Feedback -->
            <span
              v-if="(item.feedback_direction ?? 0) > 0"
              class="flex items-center gap-0.5 text-[10px] text-green-600 flex-shrink-0"
            >
              <Icon name="heroicons-hand-thumb-up" class="w-3 h-3" />
              Good
            </span>
            <span
              v-else-if="(item.feedback_direction ?? 0) < 0"
              class="flex items-center gap-0.5 text-[10px] text-red-400 flex-shrink-0"
            >
              <Icon name="heroicons-hand-thumb-down" class="w-3 h-3" />
              Bad
            </span>
            <!-- Tool counts -->
            <span class="flex items-center gap-0.5 text-[10px] text-green-600 flex-shrink-0">
              <Icon name="heroicons-check-circle" class="w-3 h-3" />
              {{ item.total_successful_tools }}
            </span>
            <span v-if="item.total_failed_tools > 0" class="flex items-center gap-0.5 text-[10px] text-red-500 flex-shrink-0">
              <Icon name="heroicons-x-circle" class="w-3 h-3" />
              {{ item.total_failed_tools }}
            </span>
            <!-- Date -->
            <span class="text-[10px] text-gray-400 dark:text-gray-500 flex-shrink-0 w-6 text-right">
              {{ formatRelative(item.created_at) }}
            </span>
          </div>
        </div>

        <!-- Pagination -->
        <div v-if="totalPages > 1" class="flex items-center justify-between mt-2 pt-1.5 border-t border-gray-100 dark:border-gray-800">
          <button
            class="flex items-center gap-0.5 text-[10px] text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 disabled:opacity-30 disabled:cursor-not-allowed"
            :disabled="currentPage === 1"
            @click.stop="currentPage--"
          >
            <Icon name="heroicons-chevron-left" class="w-3 h-3" />
            Prev
          </button>
          <span class="text-[10px] text-gray-400 dark:text-gray-500">{{ currentPage }} / {{ totalPages }}</span>
          <button
            class="flex items-center gap-0.5 text-[10px] text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 disabled:opacity-30 disabled:cursor-not-allowed"
            :disabled="currentPage === totalPages"
            @click.stop="currentPage++"
          >
            Next
            <Icon name="heroicons-chevron-right" class="w-3 h-3" />
          </button>
        </div>

        <!-- Empty filtered state -->
        <div v-if="filteredExecutions.length === 0" class="text-center py-3 text-[11px] text-gray-400 dark:text-gray-500">
          {{ $t('tools.listAgentExecutions.noMatch') }}
        </div>
      </div>
    </Transition>

    <!-- Empty state when success but no results -->
    <Transition name="fade">
      <div v-if="isExpanded && isSuccess && executions.length === 0" class="mt-2 text-[11px] text-gray-400 dark:text-gray-500 ps-1">
        {{ $t('tools.listAgentExecutions.empty') }}
      </div>
    </Transition>

    <!-- Trace modal for clicked row -->
    <TraceModal
      v-model="showTrace"
      :report-id="traceReportId"
      :completion-id="traceCompletionId"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import TraceModal from '~/components/console/TraceModal.vue'

const { t } = useI18n()

const PAGE_SIZE = 7

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

interface ExecutionItem {
  agent_execution_id: string
  completion_id: string | null
  prompt: string | null
  status: string
  feedback_direction: number | null
  feedback_message: string | null
  total_tools: number
  total_failed_tools: number
  total_successful_tools: number
  step_titles: string[]
  tool_names: string[]
  report_id: string
  report_name: string | null
  user_name: string | null
  created_at: string
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const isExpanded = ref(true)
const currentPage = ref(1)
const showTrace = ref(false)
const traceReportId = ref('')
const traceCompletionId = ref('')

const status = computed(() => props.toolExecution?.status || '')
const isSuccess = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === true
})

const output = computed(() => props.toolExecution?.result_json || { executions: [], total: 0 })
const executions = computed<ExecutionItem[]>(() => output.value.executions || [])
const appliedFilter = computed(() => props.toolExecution?.arguments_json?.filter || null)

// Reset to page 1 when executions change
watch(executions, () => { currentPage.value = 1 })

const filteredExecutions = computed(() => executions.value)

const totalPages = computed(() => Math.max(1, Math.ceil(filteredExecutions.value.length / PAGE_SIZE)))

const pagedExecutions = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return filteredExecutions.value.slice(start, start + PAGE_SIZE)
})

function toolLabel(name: string): string {
  const map: Record<string, string> = {
    create_data: 'query',
    create_artifact: 'text',
    create_dashboard: 'dashboard',
    read_query: 'read query',
    describe_tables: 'describe',
    search_instructions: 'instructions',
    edit_instruction: 'edit instr',
    create_instruction: 'new instr',
    run_eval: 'eval',
  }
  return map[name] ?? name.replace(/_/g, ' ')
}

function filterLabel(value: string): string {
  switch (value) {
    case 'negative_feedback': return t('tools.listAgentExecutions.tabNegative')
    case 'failed_queries': return t('tools.listAgentExecutions.tabFailed')
    case 'low_confidence': return t('tools.listAgentExecutions.tabLowConfidence')
    case 'low_instruction_coverage': return t('tools.listAgentExecutions.tabLowCoverage')
    default: return value
  }
}

function openTrace(item: ExecutionItem) {
  traceReportId.value = item.report_id
  traceCompletionId.value = item.completion_id || ''
  showTrace.value = true
}

function toggleExpanded() {
  if (status.value !== 'running') isExpanded.value = !isExpanded.value
}

function formatRelative(iso: string): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 60) return `${mins}m`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h`
  const days = Math.floor(hrs / 24)
  return `${days}d`
}
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}

@keyframes shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: 100% 0; }
}

.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

.slide-enter-active, .slide-leave-active {
  transition: all 0.15s ease;
  overflow: hidden;
}
.slide-enter-from, .slide-leave-to { opacity: 0; max-height: 0; }
.slide-enter-to, .slide-leave-from { opacity: 1; max-height: 1000px; }
</style>
