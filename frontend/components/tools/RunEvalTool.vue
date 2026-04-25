<template>
  <div class="mt-1">
    <!-- Header line -->
    <div class="mb-2 flex items-center text-xs text-gray-500">
      <span v-if="status === 'running' || isInProgress" class="tool-shimmer flex items-center">
        <Icon name="heroicons-play" class="w-3 h-3 me-1 text-gray-400" />
        Running eval{{ totalLabel }}
      </span>
      <span v-else-if="status === 'stopped' || progress.status === 'stopped'" class="text-gray-700 flex items-center">
        <Icon name="heroicons-stop-circle" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">Eval run stopped</span>
      </span>
      <span v-else class="text-gray-700 flex items-center">
        <Icon name="heroicons-check-circle" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">Eval run finished</span>
      </span>

      <!-- Live counters -->
      <span v-if="progress.total > 0" class="ms-2 text-[10px] text-gray-500">
        {{ progress.finished }} / {{ progress.total }}
        <span v-if="progress.passed > 0" class="ms-1 text-green-700">· {{ progress.passed }} pass</span><span
          v-if="progress.failed > 0" class="ms-1 text-red-700">· {{ progress.failed }} fail</span>
      </span>

      <!-- Stop button (only while in-flight) -->
      <button
        v-if="isInProgress && systemCompletionId"
        class="ms-auto inline-flex items-center gap-0.5 text-[10px] text-red-600 hover:text-red-800"
        @click="stopRun"
        :disabled="isStopping"
        title="Stop the run (also stops the surrounding agent loop)"
      >
        <Icon name="heroicons-stop" class="w-3 h-3" />
        <span>{{ isStopping ? 'Stopping…' : 'Stop' }}</span>
      </button>
    </div>

    <!-- Progress bar -->
    <div v-if="progress.total > 0" class="mb-2">
      <div class="h-1 bg-gray-100 rounded overflow-hidden">
        <div
          class="h-full transition-all duration-300"
          :class="failedAny ? 'bg-amber-400' : 'bg-green-400'"
          :style="{ width: `${pctFinished}%` }"
        />
      </div>
    </div>

    <!-- Per-case rows -->
    <ul v-if="progress.cases.length" class="text-xs text-gray-600 ms-1 space-y-1 leading-snug">
      <li v-for="c in progress.cases" :key="c.case_id" class="flex items-center py-0.5 px-1 rounded">
        <Icon
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
import { computed, ref } from 'vue'

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

// Reactive view over the live progress object the parent maintains. When
// the tool finishes we fall back to the final ``result_json`` summary so
// the bubble keeps rendering correct totals after ``tool.progress``
// events stop arriving.
const progress = computed<EvalProgress>(() => {
  const live = (props.toolExecution as any)?.eval_progress as EvalProgress | undefined
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

const pctFinished = computed(() => {
  const total = Math.max(progress.value.total || 0, 0)
  if (total === 0) return 0
  return Math.min(100, Math.round((progress.value.finished / total) * 100))
})

const totalLabel = computed(() => {
  const total = progress.value.total
  if (!total) return ''
  return ` · ${total} case${total === 1 ? '' : 's'}`
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
  if (s === 'stopped') return 'text-gray-600'
  return 'text-gray-500'
}

async function stopRun() {
  if (!props.systemCompletionId || isStopping.value) return
  isStopping.value = true
  try {
    // Sigkill the parent system completion. Inside the agent, run_eval's
    // polling loop detects the parent stop and cascades a TestRun.stop.
    await useMyFetch(`/api/completions/${props.systemCompletionId}/sigkill`, { method: 'POST' })
  } catch (e) {
    console.error('Failed to stop eval run via parent sigkill:', e)
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
