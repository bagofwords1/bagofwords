<template>
  <span v-if="loaded && data" class="inline-flex items-center text-[11px]" @click.stop>
    <!-- Global instruction: count + link, nothing to run inline -->
    <template v-if="data.is_global">
      <span class="inline-flex items-center gap-1 text-gray-500 dark:text-gray-400">
        <UIcon name="i-heroicons-beaker" class="w-3.5 h-3.5 text-violet-400" />
        {{ data.case_count }} eval{{ data.case_count === 1 ? '' : 's' }} · {{ data.agent_count }} agent{{ data.agent_count === 1 ? '' : 's' }}
      </span>
      <NuxtLink to="/evals" class="ms-1.5 text-blue-600 dark:text-blue-400 hover:underline">View all</NuxtLink>
    </template>

    <!-- Scoped with cases: compact Run-eval button + chevron popover -->
    <template v-else-if="data.case_count > 0">
      <div class="inline-flex items-center rounded-md border border-gray-200 dark:border-gray-800 overflow-hidden bg-white dark:bg-gray-900">
        <button type="button" class="inline-flex items-center gap-1 px-2 h-6 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
          :disabled="runState === 'running'" @click.stop="runEval">
          <Spinner v-if="runState === 'running'" class="w-3 h-3 text-violet-600" />
          <UIcon v-else name="i-heroicons-beaker" class="w-3.5 h-3.5 text-violet-500" />
          <span v-if="runState === 'running'" class="text-gray-600 dark:text-gray-400">Running {{ doneCount }}/{{ data.case_count }}</span>
          <span v-else-if="runState === 'done'" class="inline-flex items-center gap-1">
            <span class="text-green-600 dark:text-green-400 font-medium">✓ {{ passed }}</span>
            <span v-if="failed" class="text-red-600 dark:text-red-400 font-medium">✗ {{ failed }}</span>
          </span>
          <span v-else class="text-gray-700 dark:text-gray-300">Run eval</span>
        </button>
        <UPopover :popper="{ placement: 'bottom-end' }">
          <button type="button" class="px-1 h-6 border-s border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50" :title="`${data.case_count} resolved eval${data.case_count === 1 ? '' : 's'}`" @click.stop>
            <UIcon name="i-heroicons-chevron-down" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
          </button>
          <template #panel>
            <div class="p-2 w-72 max-h-72 overflow-auto" @click.stop>
              <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1.5 px-0.5">
                {{ data.case_count }} eval{{ data.case_count === 1 ? '' : 's' }} for this agent
                <span v-if="buildId" class="normal-case text-gray-400 dark:text-gray-500">· pinned to this change</span>
              </div>
              <div v-for="c in data.cases" :key="c.id" class="flex items-center gap-1.5 py-1 px-0.5 text-[11px]">
                <span class="w-3 text-center shrink-0" :class="statusColor(c.id)">{{ statusSymbol(c.id) }}</span>
                <span class="truncate text-gray-700 dark:text-gray-300">{{ c.name }}</span>
              </div>
            </div>
          </template>
        </UPopover>
      </div>
    </template>

    <!-- No evals resolved: grayed, disabled -->
    <template v-else>
      <span class="inline-flex items-center gap-1 px-2 h-6 rounded-md border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/40 text-gray-300 dark:text-gray-600 cursor-not-allowed"
        title="No evals resolved for this agent yet">
        <UIcon name="i-heroicons-beaker" class="w-3.5 h-3.5" />
        Run eval
      </span>
    </template>
  </span>
</template>

<script setup lang="ts">
// Compact resolved-eval control: a "Run eval" button + chevron popover listing
// the eval cases that resolve for the instruction's agent. When `buildId` is
// given the run is pinned to that suggestion/draft build, so it measures exactly
// that change in isolation.
import Spinner from '~/components/Spinner.vue'
const props = defineProps<{ instructionId: string; buildId?: string | null }>()

const data = ref<any>(null)
const loaded = ref(false)
const runState = ref<'idle' | 'running' | 'done'>('idle')
const passed = ref(0)
const failed = ref(0)
const doneCount = ref(0)
const caseStatus = ref<Record<string, string>>({})

function statusSymbol(id: string) {
  const s = caseStatus.value[id]
  if (s === 'pass') return '✓'
  if (s === 'fail' || s === 'error') return '✗'
  if (runState.value === 'running') return '·'
  return '·'
}
function statusColor(id: string) {
  const s = caseStatus.value[id]
  if (s === 'pass') return 'text-green-600'
  if (s === 'fail' || s === 'error') return 'text-red-600'
  return 'text-gray-300'
}

async function load() {
  loaded.value = false
  try {
    const res = await useMyFetch<any>(`/instructions/${props.instructionId}/resolved-evals`, { method: 'GET' })
    data.value = res.data.value
  } catch (e) { data.value = null }
  loaded.value = true
}

async function runEval() {
  if (!data.value?.cases?.length) return
  runState.value = 'running'; passed.value = 0; failed.value = 0; doneCount.value = 0; caseStatus.value = {}
  try {
    const body: any = { case_ids: data.value.cases.map((c: any) => c.id), trigger_reason: 'resolved_eval' }
    if (props.buildId) body.build_id = props.buildId
    const res: any = await useMyFetch('/api/tests/runs', { method: 'POST', body })
    const run = res?.data?.value
    if (!run?.id) { runState.value = 'idle'; return }
    await poll(run.id)
  } catch (e) {
    runState.value = 'idle'
  }
}

async function poll(runId: string) {
  const deadline = Date.now() + 8 * 60 * 1000
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 2000))
    try {
      const res: any = await useMyFetch(`/api/tests/runs/${runId}/results`)
      const results: any[] = res?.data?.value || []
      const next: Record<string, string> = {}
      for (const r of results) next[r.case_id] = r.status
      caseStatus.value = next
      passed.value = results.filter(r => r.status === 'pass').length
      failed.value = results.filter(r => r.status === 'fail' || r.status === 'error').length
      doneCount.value = results.filter(r => ['pass', 'fail', 'error', 'stopped'].includes(r.status)).length
      if (doneCount.value >= (data.value?.case_count || 0) && doneCount.value > 0) break
    } catch (e) { /* keep polling */ }
  }
  runState.value = 'done'
}

watch(() => [props.instructionId, props.buildId], load)
onMounted(load)
</script>
