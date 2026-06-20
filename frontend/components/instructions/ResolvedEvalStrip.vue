<template>
  <div v-if="loaded && (data?.case_count || data?.is_global)" class="rounded-md border border-gray-200 bg-gray-50/60 px-3 py-2">
    <div class="flex items-center justify-between gap-3">
      <div class="flex items-center gap-2 min-w-0">
        <UIcon name="i-heroicons-beaker" class="w-4 h-4 text-violet-500 shrink-0" />
        <span v-if="data.is_global" class="text-xs text-gray-700 truncate">
          <span class="font-medium">{{ data.case_count }}</span> eval{{ data.case_count === 1 ? '' : 's' }}
          across <span class="font-medium">{{ data.agent_count }}</span> agent{{ data.agent_count === 1 ? '' : 's' }}
        </span>
        <span v-else class="text-xs text-gray-700 truncate">
          <span class="font-medium">{{ data.case_count }}</span> eval{{ data.case_count === 1 ? '' : 's' }} resolved for this agent
          <span v-if="buildId" class="text-gray-400">· pinned to this change</span>
        </span>
        <!-- last/live status -->
        <span v-if="runState === 'running'" class="inline-flex items-center gap-1 text-[11px] text-blue-600">
          <Spinner class="w-3 h-3 animate-spin" /> running {{ doneCount }}/{{ data.case_count }}
        </span>
        <span v-else-if="runState === 'done'" class="inline-flex items-center gap-1.5 text-[11px]">
          <span class="text-green-600 font-medium">✓ {{ passed }}</span>
          <span v-if="failed" class="text-red-600 font-medium">✗ {{ failed }}</span>
          <span class="text-gray-400">{{ ranAtLabel }}</span>
        </span>
      </div>
      <div class="shrink-0">
        <NuxtLink v-if="data.is_global" to="/evals" class="text-xs text-blue-600 hover:underline">View all</NuxtLink>
        <button v-else-if="data.case_count > 0" type="button"
          class="h-6 px-2 rounded-md bg-violet-600 text-white text-[11px] font-medium hover:bg-violet-700 disabled:opacity-50 inline-flex items-center gap-1"
          :disabled="runState === 'running'" @click="runEval">
          <UIcon name="i-heroicons-play" class="w-3 h-3" />
          {{ runState === 'running' ? 'Running…' : 'Run eval' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// Surfaces the eval cases that "resolve" for an instruction (scoped to its
// agent[s]) and lets the user run them. When `buildId` is given the run is
// pinned to that suggestion build, so it measures exactly that change in
// isolation — not main, and not a sibling suggestion on the same instruction.
const props = defineProps<{ instructionId: string; buildId?: string | null }>()

const data = ref<any>(null)
const loaded = ref(false)
const runState = ref<'idle' | 'running' | 'done'>('idle')
const passed = ref(0)
const failed = ref(0)
const doneCount = ref(0)
const ranAt = ref<number | null>(null)

const ranAtLabel = computed(() => ranAt.value ? 'just now' : '')

async function load() {
  loaded.value = false
  try {
    const res = await useMyFetch<any>(`/instructions/${props.instructionId}/resolved-evals`, { method: 'GET' })
    data.value = res.data.value
  } catch (e) { data.value = null }
  loaded.value = true
}

async function runEval() {
  if (!data.value || !data.value.cases?.length) return
  runState.value = 'running'; passed.value = 0; failed.value = 0; doneCount.value = 0
  try {
    const body: any = {
      case_ids: data.value.cases.map((c: any) => c.id),
      trigger_reason: 'resolved_eval',
    }
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
      passed.value = results.filter(r => r.status === 'pass').length
      failed.value = results.filter(r => r.status === 'fail' || r.status === 'error').length
      doneCount.value = results.filter(r => ['pass', 'fail', 'error', 'stopped'].includes(r.status)).length
      if (doneCount.value >= (data.value?.case_count || 0) && doneCount.value > 0) break
    } catch (e) { /* keep polling */ }
  }
  runState.value = 'done'; ranAt.value = Date.now()
}

watch(() => [props.instructionId, props.buildId], load)
onMounted(load)
</script>
