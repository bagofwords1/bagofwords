<template>
  <div class="mt-1">
    <!-- Header line -->
    <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
      <span v-if="status === 'running' || isInProgress" class="tool-shimmer flex items-center">
        <Spinner class="w-3 h-3 me-1 text-gray-400" />
        {{ t('tools.runEval.running') }}{{ totalLabel }}
      </span>
      <span v-else-if="status === 'stopped' || progress.status === 'stopped'" class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-stop-circle" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">{{ t('tools.runEval.stopped') }}</span>
      </span>
      <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-check-circle" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">{{ t('tools.runEval.finished') }}</span>
      </span>

      <!-- Live counters -->
      <span v-if="progress.total > 0" class="ms-2 text-[10px] text-gray-500 dark:text-gray-400">
        <!-- dir=ltr: keep "finished / total" from bidi-reversing under RTL -->
        <span dir="ltr">{{ progress.finished }} / {{ progress.total }}</span>
        <span v-if="progress.passed > 0" class="ms-1 text-green-700">· {{ t('tools.runEval.pass', { count: progress.passed }) }}</span><span
          v-if="progress.failed > 0" class="ms-1 text-red-700">· {{ t('tools.runEval.fail', { count: progress.failed }) }}</span>
      </span>

      <!-- Stop button (only while in-flight) -->
      <button
        v-if="canStop"
        class="ms-auto inline-flex items-center gap-0.5 text-[10px] text-red-600 hover:text-red-800"
        @click="stopRun"
        :disabled="isStopping"
        :title="t('tools.runEval.stopTitle')"
      >
        <Icon name="heroicons-stop" class="w-3 h-3" />
        <span>{{ isStopping ? t('tools.runEval.stopping') : t('tools.runEval.stop') }}</span>
      </button>
    </div>

    <!-- Progress bar — only while running; once finished the counts + per-case
         rows carry the result, and a solid full-width bar reads as a heavy
         divider. Kept thin and capped in width so it stays subtle. -->
    <div v-if="progress.total > 0 && isInProgress" class="mb-2">
      <div class="h-0.5 w-40 max-w-full bg-gray-100 dark:bg-gray-800 rounded overflow-hidden">
        <div
          class="h-full bg-green-400 transition-all duration-300"
          :style="{ width: `${pctFinished}%` }"
        />
      </div>
    </div>

    <!-- Per-case rows -->
    <ul v-if="progress.cases.length" class="text-xs text-gray-600 dark:text-gray-400 ms-1 space-y-1 leading-snug">
      <li v-for="c in progress.cases" :key="c.case_id" class="flex items-center py-0.5 px-1 rounded">
        <Spinner
          v-if="c.status === 'in_progress'"
          class="w-3 h-3 me-1 flex-shrink-0 text-blue-400"
        />
        <Icon
          v-else
          :name="caseIcon(c.status)"
          class="w-3 h-3 me-1 flex-shrink-0"
          :class="caseIconColor(c.status)"
        />
        <span class="truncate" :title="c.case_name || c.case_id">{{ c.case_name || c.case_id }}</span>
        <span class="ms-2 text-[10px] flex-shrink-0" :class="caseStatusColor(c.status)">{{ c.status }}</span>
        <span v-if="c.failure_reason" class="ms-2 text-[10px] text-gray-400 truncate" :title="c.failure_reason">
          — {{ c.failure_reason }}
        </span>
      </li>
    </ul>

    <!-- Run-id link -->
    <div v-if="progress.run_id" class="mt-1 text-[10px] text-gray-400 ms-1">
      <NuxtLink :to="`/evals/runs/${progress.run_id}`" class="hover:text-blue-600 inline-flex items-center gap-0.5">
        <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
        Open run
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
  // Live, mutated by handleEvalProgress in the parent on tool.progress events.
  eval_progress?: EvalProgress | null
}

interface EvalCaseRow {
  case_id: string
  case_name?: string
  status: string
  failure_reason?: string | null
}

interface EvalProgress {
  run_id: string | null
  total: number
  finished: number
  passed: number
  failed: number
  status: string
  cases: EvalCaseRow[]
}

const props = defineProps<{
  toolExecution: ToolExecution
  systemCompletionId?: string | null
}>()

const status = computed(() => props.toolExecution?.status || '')
const isStopping = ref(false)

// Snapshot fetched from the run API for detached (background) runs — the
// tool call already ended, so ``tool.progress`` events will never arrive
// and the run's live state only exists server-side.
const polled = ref<EvalProgress | null>(null)

// Reactive view over the live progress object the parent maintains. When
// the tool finishes we fall back to the polled run snapshot (background
// runs), then the final ``result_json`` summary so the bubble keeps
// rendering correct totals after ``tool.progress`` events stop arriving.
const progress = computed<EvalProgress>(() => {
  const live = (props.toolExecution as any)?.eval_progress as EvalProgress | undefined
  if (live && status.value === 'running') return live
  if (polled.value) return polled.value
  if (live) return live
  const rj: any = props.toolExecution?.result_json || {}
  const cases: EvalCaseRow[] = Array.isArray(rj.results)
    ? rj.results.map((r: any) => ({
        case_id: r.case_id,
        case_name: r.case_name,
        status: r.status || '',
        failure_reason: r.failure_reason || null,
      }))
    : []
  return {
    run_id: rj.run_id || null,
    total: typeof rj.total === 'number' ? rj.total : cases.length,
    finished: typeof rj.finished === 'number' ? rj.finished : cases.filter(c => c.status && c.status !== 'init' && c.status !== 'in_progress').length,
    passed: typeof rj.passed === 'number' ? rj.passed : cases.filter(c => c.status === 'pass').length,
    failed: typeof rj.failed === 'number' ? rj.failed : cases.filter(c => c.status === 'fail' || c.status === 'error').length,
    status: rj.status || '',
    cases,
  }
})

const isInProgress = computed(() => {
  if (status.value === 'running') return true
  const s = progress.value.status
  return !s || s === 'in_progress'
})

const failedAny = computed(() => progress.value.failed > 0 || progress.value.status === 'error')

const canStop = computed(() =>
  (isInProgress.value && !!props.systemCompletionId) || isDetachedInProgress.value
)

const pctFinished = computed(() => {
  const total = Math.max(progress.value.total || 0, 0)
  if (total === 0) return 0
  return Math.min(100, Math.round((progress.value.finished / total) * 100))
})

const totalLabel = computed(() => {
  const total = progress.value.total
  if (!total) return ''
  const label = total === 1 ? t('tools.runEval.caseSingular') : t('tools.runEval.casePlural')
  return t('tools.runEval.totalLabel', { count: total, label })
})

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
  if (s === 'in_progress') return 'text-blue-400 animate-spin-slow'
  return 'text-gray-400'
}
function caseStatusColor(s: string): string {
  if (s === 'pass') return 'text-green-700'
  if (s === 'fail' || s === 'error') return 'text-red-700'
  if (s === 'stopped') return 'text-gray-600 dark:text-gray-400'
  return 'text-gray-500 dark:text-gray-400'
}

// --- Background-run polling -------------------------------------------------
// A detached run keeps executing server-side after the tool call ends. Follow
// it via the run API (same source the run detail page uses) so the card's
// counters keep ticking without any tool events.
const TERMINAL_RUN = new Set(['success', 'error', 'stopped'])
let pollTimer: ReturnType<typeof setInterval> | null = null

const isDetachedInProgress = computed(() => {
  if (status.value === 'running') return false
  const rj: any = props.toolExecution?.result_json || {}
  if (!rj.detached || !rj.run_id) return false
  const current = polled.value?.status || rj.status || 'in_progress'
  return !TERMINAL_RUN.has(current)
})

async function pollRunOnce() {
  const rj: any = props.toolExecution?.result_json || {}
  const runId = rj.run_id
  if (!runId) return
  try {
    const [runRes, resultsRes]: any[] = await Promise.all([
      useMyFetch(`/api/tests/runs/${runId}`),
      useMyFetch(`/api/tests/runs/${runId}/results`),
    ])
    const run = runRes?.data?.value
    const results = (resultsRes?.data?.value || []) as any[]
    if (!run) return
    const terminalCase = new Set(['pass', 'fail', 'error', 'stopped'])
    const byCase: Record<string, string> = {}
    for (const c of (rj.results || [])) byCase[c.case_id] = c.case_name || ''
    const cases: EvalCaseRow[] = results.map((r: any) => ({
      case_id: r.case_id,
      case_name: byCase[r.case_id] || r.case_id,
      status: r.status || '',
      failure_reason: r.failure_reason || null,
    }))
    polled.value = {
      run_id: String(runId),
      total: cases.length || (typeof rj.total === 'number' ? rj.total : 0),
      finished: cases.filter(c => terminalCase.has(c.status)).length,
      passed: cases.filter(c => c.status === 'pass').length,
      failed: cases.filter(c => c.status === 'fail' || c.status === 'error').length,
      status: run.status || 'in_progress',
      cases,
    }
    if (TERMINAL_RUN.has(run.status)) stopPolling()
  } catch (e) {
    // Transient — keep polling; the run page remains the fallback view.
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
  if (isDetachedInProgress.value) startPolling()
})
watch(isDetachedInProgress, (v) => {
  if (v) startPolling()
})
onBeforeUnmount(stopPolling)

async function stopRun() {
  if (isStopping.value) return
  isStopping.value = true
  try {
    const rj: any = props.toolExecution?.result_json || {}
    if (rj.detached && rj.run_id) {
      // Background run: the tool call already ended, so sigkilling the parent
      // completion would do nothing — stop the TestRun directly.
      await useMyFetch(`/api/tests/runs/${rj.run_id}/stop`, { method: 'POST' })
      await pollRunOnce()
    } else if (props.systemCompletionId) {
      // Attached run: sigkill the parent system completion. Inside the agent,
      // run_eval's polling loop detects the parent stop and cascades a
      // TestRun.stop.
      await useMyFetch(`/api/completions/${props.systemCompletionId}/sigkill`, { method: 'POST' })
    }
  } catch (e) {
    console.error('Failed to stop eval run:', e)
  } finally {
    isStopping.value = false
  }
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
.animate-spin-slow {
  animation: spin 2s linear infinite;
}
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
