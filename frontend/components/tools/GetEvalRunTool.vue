<template>
  <div class="mt-1">
    <div
      class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400"
      :class="{ 'cursor-pointer select-none': canExpand }"
      @click="canExpand && (expanded = !expanded)"
    >
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-document-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
        {{ t('tools.getEvalRun.reading') }}
      </span>
      <span v-else-if="success" class="text-gray-700 dark:text-gray-300 flex items-center min-w-0">
        <Icon name="heroicons-document-magnifying-glass" class="w-3 h-3 me-1 text-purple-400 flex-shrink-0" />
        <span class="align-middle flex-shrink-0">{{ t('tools.getEvalRun.read') }}</span>
        <span v-if="evalTitle" class="ms-1 truncate font-medium text-gray-700 dark:text-gray-300" :title="evalTitle">{{ evalTitle }}</span>
        <span class="ms-1.5 flex-shrink-0 inline-flex px-1.5 py-0.5 rounded-full text-[10px] font-medium" :class="runStatusClass(runStatus)">{{ runStatus }}</span>
        <span v-if="total > 0" class="ms-1.5 flex-shrink-0 text-[10px] text-gray-500 dark:text-gray-400">
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

      <!-- Expand/collapse affordance — only when there are per-case rows to show -->
      <Icon
        v-if="canExpand"
        name="heroicons-chevron-down"
        class="w-3 h-3 ms-1 text-gray-400 transition-transform duration-200"
        :class="{ '-rotate-90': !expanded }"
      />
    </div>

    <!-- Compare summary (compare_to_previous=true) -->
    <div v-if="compareSummary && expanded" class="mb-1 ms-1 flex items-center gap-1.5 text-[10px]">
      <Icon name="heroicons:arrows-right-left" class="w-3 h-3 text-gray-400" />
      <span class="text-gray-500 dark:text-gray-400">{{ t('tools.getEvalRun.vsPrevious') }}</span>
      <span class="inline-flex px-1.5 py-0.5 rounded bg-green-100 dark:bg-green-900/50 text-green-800">{{ t('tools.getEvalRun.fixed', { count: compareSummary.fixed || 0 }) }}</span>
      <span class="inline-flex px-1.5 py-0.5 rounded bg-red-100 dark:bg-red-900/50 text-red-800">{{ t('tools.getEvalRun.regressed', { count: compareSummary.regressed || 0 }) }}</span>
    </div>

    <!-- Per-case rows (collapsed by default) -->
    <ul v-if="cases.length && expanded" class="text-xs text-gray-600 dark:text-gray-400 ms-1 space-y-1 leading-snug">
      <li v-for="c in cases" :key="c.case_id" class="flex items-center py-0.5 px-1 rounded">
        <Spinner v-if="c.status === 'in_progress'" class="w-3 h-3 me-1 flex-shrink-0 text-blue-400" />
        <Icon v-else :name="caseIcon(c.status)" class="w-3 h-3 me-1 flex-shrink-0" :class="caseIconColor(c.status)" />
        <span class="truncate" :title="c.case_name || c.case_id">{{ c.case_name || c.case_id }}</span>
        <span class="ms-2 text-[10px] flex-shrink-0" :class="caseStatusColor(c.status)">{{ c.status }}</span>
        <span v-if="c.failure_reason" class="ms-2 text-[10px] text-gray-400 truncate" :title="c.failure_reason">— {{ c.failure_reason }}</span>
      </li>
    </ul>

    <div v-if="runId && expanded" class="mt-1 text-[10px] text-gray-400 ms-1">
      <NuxtLink :to="`/evals/runs/${runId}`" class="hover:text-blue-600 inline-flex items-center gap-0.5">
        <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
        {{ t('tools.getEvalRun.openRun') }}
      </NuxtLink>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

interface PolledRun {
  status: string
  total: number
  finished: number
  passed: number
  failed: number
  cases: any[]
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const success = computed<boolean>(() => !!result.value?.success)
const runId = computed<string>(() => result.value?.run_id || '')

// ``get_eval_run`` returns a point-in-time snapshot: whatever the run's state
// was when the agent read it. If the agent read the run while it was still
// executing, that snapshot is frozen at ``in_progress`` and the card would
// keep showing a spinning "in_progress" forever, even after the run finished.
// Follow the run to its terminal state (same source as the run detail page)
// so a stale in-progress read self-heals to the real outcome.
const polled = ref<PolledRun | null>(null)

const runStatus = computed<string>(() => polled.value?.status || result.value?.status || '')
const total = computed<number>(() => polled.value?.total ?? result.value?.total ?? 0)
const finished = computed<number>(() => polled.value?.finished ?? result.value?.finished ?? 0)
const passed = computed<number>(() => polled.value?.passed ?? result.value?.passed ?? 0)
const failed = computed<number>(() => polled.value?.failed ?? result.value?.failed ?? 0)
const message = computed<string>(() => result.value?.message || '')
const cases = computed<any[]>(() =>
  polled.value?.cases || (Array.isArray(result.value?.results) ? result.value.results : [])
)
const compareSummary = computed<any | null>(() => result.value?.compare?.summary || null)

// The eval under test. For the common single-case run, show that case's name
// in the header so each card says *which* eval it ran; multi-case runs keep
// the generic label (the names live in the expanded rows).
const evalTitle = computed<string>(() => {
  const cs = cases.value
  if (cs.length === 1) return cs[0]?.case_name || cs[0]?.case_id || ''
  return ''
})

// Collapsed by default; the header stays a single summary line and the
// per-case rows (and compare row) expand on click. Only offer the toggle when
// there's actually detail to reveal.
const expanded = ref(false)
const canExpand = computed<boolean>(() => success.value && (cases.value.length > 0 || !!compareSummary.value || !!runId.value))

// --- Follow a still-running read to completion -----------------------------
const TERMINAL_RUN = new Set(['success', 'error', 'stopped'])
let pollTimer: ReturnType<typeof setInterval> | null = null

const isInProgress = computed<boolean>(() => {
  if (!success.value || !runId.value) return false
  const s = polled.value?.status || result.value?.status || ''
  return !!s && !TERMINAL_RUN.has(s)
})

async function pollRunOnce() {
  const rid = runId.value
  if (!rid) return
  try {
    const [runRes, resultsRes]: any[] = await Promise.all([
      useMyFetch(`/api/tests/runs/${rid}`),
      useMyFetch(`/api/tests/runs/${rid}/results`),
    ])
    const run = runRes?.data?.value
    const results = (resultsRes?.data?.value || []) as any[]
    if (!run) return
    const terminalCase = new Set(['pass', 'fail', 'error', 'stopped'])
    // Preserve case names from the original snapshot; the results endpoint
    // only carries ids + statuses.
    const byCase: Record<string, string> = {}
    for (const c of (result.value?.results || [])) byCase[c.case_id] = c.case_name || ''
    const mapped = results.map((r: any) => ({
      case_id: r.case_id,
      case_name: byCase[r.case_id] || r.case_id,
      status: r.status || '',
      failure_reason: r.failure_reason || null,
    }))
    polled.value = {
      status: run.status || 'in_progress',
      total: mapped.length || total.value,
      finished: mapped.filter(c => terminalCase.has(c.status)).length,
      passed: mapped.filter(c => c.status === 'pass').length,
      failed: mapped.filter(c => c.status === 'fail' || c.status === 'error').length,
      cases: mapped,
    }
    if (TERMINAL_RUN.has(run.status)) stopPolling()
  } catch (e) {
    // Transient — keep polling; the run detail page remains the fallback view.
  }
}

function startPolling() {
  if (pollTimer) return
  pollRunOnce()
  pollTimer = setInterval(pollRunOnce, 4000)
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(() => {
  if (isInProgress.value) startPolling()
})
watch(isInProgress, (v) => {
  if (v) startPolling()
  else stopPolling()
})
onBeforeUnmount(stopPolling)

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
