<template>
  <div class="mt-1">
    <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-document-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
        {{ t('tools.getEvalRun.reading') }}
      </span>
      <span v-else-if="success" class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-document-magnifying-glass" class="w-3 h-3 me-1 text-purple-400" />
        <span class="align-middle">{{ t('tools.getEvalRun.read') }}</span>
        <span class="ms-1.5 inline-flex px-1.5 py-0.5 rounded-full text-[10px] font-medium" :class="runStatusClass(runStatus)">{{ runStatus }}</span>
        <span v-if="total > 0" class="ms-1.5 text-[10px] text-gray-500 dark:text-gray-400">
          <!-- dir=ltr: keep "finished / total" from bidi-reversing under RTL -->
          <span dir="ltr">{{ finished }} / {{ total }}</span>
          <span v-if="passed > 0" class="ms-1 text-green-700">· {{ t('tools.getEvalRun.pass', { count: passed }) }}</span>
          <span v-if="failed > 0" class="ms-1 text-red-700">· {{ t('tools.getEvalRun.fail', { count: failed }) }}</span>
        </span>
      </span>
      <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">{{ message || t('tools.getEvalRun.notFound') }}</span>
      </span>
    </div>

    <!-- Compare summary (compare_to_previous=true) -->
    <div v-if="compareSummary" class="mb-1 ms-1 flex items-center gap-1.5 text-[10px]">
      <Icon name="heroicons:arrows-right-left" class="w-3 h-3 text-gray-400" />
      <span class="text-gray-500 dark:text-gray-400">{{ t('tools.getEvalRun.vsPrevious') }}</span>
      <span class="inline-flex px-1.5 py-0.5 rounded bg-green-100 dark:bg-green-900/50 text-green-800">{{ t('tools.getEvalRun.fixed', { count: compareSummary.fixed || 0 }) }}</span>
      <span class="inline-flex px-1.5 py-0.5 rounded bg-red-100 dark:bg-red-900/50 text-red-800">{{ t('tools.getEvalRun.regressed', { count: compareSummary.regressed || 0 }) }}</span>
    </div>

    <!-- Per-case rows -->
    <ul v-if="cases.length" class="text-xs text-gray-600 dark:text-gray-400 ms-1 space-y-1 leading-snug">
      <li v-for="c in cases" :key="c.case_id" class="flex items-center py-0.5 px-1 rounded">
        <Icon :name="caseIcon(c.status)" class="w-3 h-3 me-1 flex-shrink-0" :class="caseIconColor(c.status)" />
        <span class="truncate" :title="c.case_name || c.case_id">{{ c.case_name || c.case_id }}</span>
        <span class="ms-2 text-[10px] flex-shrink-0" :class="caseStatusColor(c.status)">{{ c.status }}</span>
        <span v-if="c.failure_reason" class="ms-2 text-[10px] text-gray-400 truncate" :title="c.failure_reason">— {{ c.failure_reason }}</span>
      </li>
    </ul>

    <div v-if="runId" class="mt-1 text-[10px] text-gray-400 ms-1">
      <NuxtLink :to="`/evals/runs/${runId}`" class="hover:text-blue-600 inline-flex items-center gap-0.5">
        <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
        {{ t('tools.getEvalRun.openRun') }}
      </NuxtLink>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const success = computed<boolean>(() => !!result.value?.success)
const runId = computed<string>(() => result.value?.run_id || '')
const runStatus = computed<string>(() => result.value?.status || '')
const total = computed<number>(() => result.value?.total || 0)
const finished = computed<number>(() => result.value?.finished || 0)
const passed = computed<number>(() => result.value?.passed || 0)
const failed = computed<number>(() => result.value?.failed || 0)
const message = computed<string>(() => result.value?.message || '')
const cases = computed<any[]>(() => Array.isArray(result.value?.results) ? result.value.results : [])
const compareSummary = computed<any | null>(() => result.value?.compare?.summary || null)

function runStatusClass(s: string): string {
  if (s === 'success') return 'bg-green-100 text-green-800'
  if (s === 'error') return 'bg-red-100 text-red-800'
  if (s === 'in_progress') return 'bg-blue-50 text-blue-700'
  return 'bg-gray-100 text-gray-700'
}
function caseIcon(s: string): string {
  if (s === 'pass') return 'heroicons-check-circle'
  if (s === 'fail' || s === 'error') return 'heroicons-x-circle'
  if (s === 'stopped') return 'heroicons-stop-circle'
  if (s === 'in_progress') return 'heroicons-arrow-path'
  return 'heroicons-clock'
}
function caseIconColor(s: string): string {
  if (s === 'pass') return 'text-green-500'
  if (s === 'fail' || s === 'error') return 'text-red-500'
  if (s === 'stopped') return 'text-gray-500'
  if (s === 'in_progress') return 'text-blue-400'
  return 'text-gray-400'
}
function caseStatusColor(s: string): string {
  if (s === 'pass') return 'text-green-700'
  if (s === 'fail' || s === 'error') return 'text-red-700'
  if (s === 'stopped') return 'text-gray-600 dark:text-gray-400'
  return 'text-gray-500 dark:text-gray-400'
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
</style>
