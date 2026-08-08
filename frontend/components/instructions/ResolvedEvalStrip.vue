<template>
  <span v-if="loaded && data" class="inline-flex items-center text-[11px]" @click.stop>
    <!-- Nothing resolves and no shelf to show: grayed, disabled -->
    <template v-if="!data.case_count && !hasShelf">
      <span class="inline-flex items-center gap-1 px-2 h-6 rounded-md border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800/40 text-gray-300 dark:text-gray-600 cursor-not-allowed"
        :title="$t('evals.strip.noneResolved')">
        <UIcon name="i-heroicons-beaker" class="w-3.5 h-3.5" />
        {{ $t('agentsPage.runEval') }}
      </span>
    </template>

    <!-- Split control: run-everything on the left, the suite tree behind the
         chevron. NOTE the wrapper deliberately has no `overflow-hidden` — the
         popover panel renders inside it, so clipping the corners would clip the
         panel to a 24px pill. The children round their own outer edges. -->
    <template v-else>
      <div class="inline-flex items-center rounded-md border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
        <button type="button"
          class="inline-flex items-center gap-1 px-2 h-6 rounded-s-md hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
          :disabled="isRunning || !data.case_count" @click.stop="runCases(allCaseIds)">
          <Spinner v-if="isRunning" class="w-3 h-3 text-violet-600" />
          <UIcon v-else name="i-heroicons-beaker" class="w-3.5 h-3.5 text-violet-500" />
          <span v-if="isRunning" class="text-gray-600 dark:text-gray-400">
            {{ $t('evals.strip.runningCount', { done: summary.done, total: summary.total }) }}
          </span>
          <!-- "✓ 1/1", not "✓ 1": a bare count says how many passed but not
               out of how many, which is the only part you actually want at a
               glance. Mirrors RunEvalTool's "finished / total" header. -->
          <span v-else-if="activeRun" class="inline-flex items-center gap-1">
            <span class="text-green-600 dark:text-green-400 font-medium" dir="ltr">✓ {{ summary.passed }}/{{ summary.total }}</span>
            <span v-if="summary.failed" class="text-red-600 dark:text-red-400 font-medium">✗ {{ summary.failed }}</span>
          </span>
          <span v-else class="text-gray-700 dark:text-gray-300">{{ $t('agentsPage.runEval') }}</span>
        </button>

        <!-- Stop, while in flight. A run started here executes server-side for
             as long as the cases take; without this the only way to end one was
             to find it on the run page. -->
        <button v-if="isRunning" type="button"
          class="inline-flex items-center px-1.5 h-6 border-s border-gray-200 dark:border-gray-800 text-red-600 hover:bg-red-50 dark:hover:bg-red-500/10 disabled:opacity-50"
          :disabled="isStopping" :title="$t('tools.runEval.stopTitle')" @click.stop="stopRun">
          <UIcon name="i-heroicons-stop" class="w-3 h-3" />
        </button>

        <!-- No @click.stop on this button: the click has to reach UPopover's
             own trigger wrapper, which is what toggles the panel. -->
        <UPopover :popper="{ placement: 'bottom-end', strategy: 'fixed' }" :ui="{ ring: '', shadow: 'shadow-lg' }">
          <button type="button"
            class="px-1 h-6 rounded-e-md border-s border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50"
            :title="$t('evals.strip.title')">
            <UIcon name="i-heroicons-chevron-down" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
          </button>

          <template #panel>
            <div class="w-80 max-h-96 overflow-auto p-2" @click.stop>
              <div class="flex items-center gap-1 px-0.5 mb-1 text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">
                <span>{{ $t('evals.strip.title') }}</span>
                <span v-if="buildId" class="normal-case ms-auto">{{ $t('evals.strip.pinned') }}</span>
              </div>

              <button v-if="data.case_count" type="button"
                class="w-full flex items-center gap-1.5 px-1.5 h-7 rounded hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
                :disabled="isRunning" @click.stop="runCases(allCaseIds)">
                <UIcon name="i-heroicons-play" class="w-3 h-3 text-blue-500 shrink-0" />
                <span class="text-[11px] font-medium text-gray-700 dark:text-gray-300">{{ $t('evals.strip.runAll') }}</span>
                <span class="ms-auto text-[10px] text-gray-400 dark:text-gray-500">{{ data.case_count }}</span>
              </button>

              <div v-for="g in data.groups" :key="g.scope" class="mt-1.5">
                <div class="flex items-center gap-1 px-0.5 text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">
                  <UIcon :name="g.scope === 'global' ? 'i-heroicons-globe-alt' : 'i-heroicons-cpu-chip'" class="w-3 h-3 shrink-0" />
                  <span class="truncate">{{ g.scope === 'global' ? $t('evals.strip.orgWide') : g.label }}</span>
                  <!-- A global instruction has no agents of its own; say so
                       rather than let the chat's agents read as the truth. -->
                  <span v-if="data.scope_source === 'report' && g.scope !== 'global'" class="normal-case shrink-0">· {{ $t('evals.strip.fromChat') }}</span>
                </div>

                <div v-if="!g.suites.length" class="px-2 py-1 text-[11px] text-gray-400 dark:text-gray-500">
                  {{ $t('agentsPage.noSuites') }}
                </div>

                <div v-for="s in g.suites" :key="s.id">
                  <button type="button"
                    class="w-full flex items-center gap-1.5 px-1.5 h-7 rounded hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50 disabled:hover:bg-transparent"
                    :disabled="isRunning || !s.case_count"
                    :title="s.case_count ? $t('evals.strip.runSuite', { name: s.name }) : $t('evals.strip.emptyShelf')"
                    @click.stop="runCases(s.cases.map((c) => c.id))">
                    <UIcon name="i-heroicons-folder" class="w-3.5 h-3.5 shrink-0" :class="s.case_count ? 'text-violet-400' : 'text-gray-300 dark:text-gray-600'" />
                    <span class="truncate text-[11px]" :class="s.case_count ? 'text-gray-700 dark:text-gray-300' : 'text-gray-400 dark:text-gray-500'">{{ s.name }}</span>
                    <span v-if="s.is_drafts" class="shrink-0 px-1 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[9px]">{{ $t('evals.strip.default') }}</span>
                    <span class="ms-auto shrink-0 inline-flex items-center gap-1 text-[10px]">
                      <template v-if="suiteRollup(s).done">
                        <span v-if="suiteRollup(s).passed" class="text-green-600 dark:text-green-400">✓ {{ suiteRollup(s).passed }}</span>
                        <span v-if="suiteRollup(s).failed" class="text-red-600 dark:text-red-400">✗ {{ suiteRollup(s).failed }}</span>
                        <!-- "/3" rather than a bare 3: next to a ✓/✗ tally an
                             unmarked number reads as a third outcome. -->
                        <span class="text-gray-400 dark:text-gray-500">/ {{ s.case_count }}</span>
                      </template>
                      <span v-else class="text-gray-400 dark:text-gray-500">{{ s.case_count }}</span>
                    </span>
                  </button>

                  <!-- One small step in from the suite, with a REAL icon in the
                       column the folder occupies — same shape as RunEvalTool's
                       per-case rows. The old `ps-7` indented past the suite
                       name and then added an invisible `·` glyph on top, so a
                       case row read as two levels deep and floated free of its
                       suite. -->
                  <div v-for="c in s.cases" :key="c.id" class="flex items-center gap-1.5 py-0.5 ps-4 pe-1.5 text-[11px]">
                    <UIcon :name="caseIcon(c.id)" class="w-3 h-3 shrink-0" :class="statusColor(c.id)" />
                    <span class="truncate text-gray-600 dark:text-gray-400">{{ c.name }}</span>
                  </div>
                </div>
              </div>

              <!-- Live run status, same shape the suggestion-review panel uses
                   so the two controls report a run identically. -->
              <div v-if="activeRun" class="mt-2 pt-2 border-t border-gray-100 dark:border-gray-800 space-y-1.5">
                <div class="flex items-center justify-between">
                  <div class="flex items-center gap-1.5">
                    <UIcon :name="isRunning ? 'i-heroicons-arrow-path' : (activeRun.status === 'success' ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle')"
                      :class="['w-3.5 h-3.5', isRunning ? 'text-blue-500 animate-spin' : (activeRun.status === 'success' ? 'text-green-500' : 'text-red-500')]" />
                    <span class="text-[11px] font-medium text-gray-700 dark:text-gray-300">{{ prettyStatus }}</span>
                  </div>
                  <NuxtLink :to="`/evals/runs/${activeRun.id}`" class="text-[10px] text-blue-500 dark:text-blue-400 hover:underline inline-flex items-center gap-0.5">
                    {{ $t('agentsPage.viewDetails') }}<UIcon name="i-heroicons-arrow-top-right-on-square" class="w-2.5 h-2.5" />
                  </NuxtLink>
                </div>
                <div class="flex flex-wrap items-center gap-1.5 text-[10px]">
                  <span class="px-1.5 py-0.5 rounded border bg-slate-50 dark:bg-slate-500/10 text-slate-600 dark:text-slate-400 border-slate-200 dark:border-slate-500/30">{{ $t('agentsPage.casesLabel') }}: {{ summary.total }}</span>
                  <span class="px-1.5 py-0.5 rounded border bg-green-50 dark:bg-green-500/10 text-green-700 dark:text-green-400 border-green-200 dark:border-green-500/30">{{ $t('agentsPage.passLabel') }}: {{ summary.passed }}</span>
                  <span class="px-1.5 py-0.5 rounded border bg-red-50 dark:bg-red-500/10 text-red-700 dark:text-red-400 border-red-200 dark:border-red-500/30">{{ $t('agentsPage.failLabel') }}: {{ summary.failed }}</span>
                  <span v-if="summary.inProgress" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border bg-blue-50 dark:bg-blue-500/10 text-blue-700 dark:text-blue-400 border-blue-200 dark:border-blue-500/30">
                    <UIcon name="i-heroicons-arrow-path" class="w-2.5 h-2.5 animate-spin" />{{ $t('agentsPage.runningLabel') }}: {{ summary.inProgress }}
                  </span>
                </div>
                <!-- Thin, capped, green — RunEvalTool's bar, for the reason
                     stated there: a solid full-width bar reads as a heavy
                     divider. Hidden below two cases, where it can only ever
                     paint 0% and then vanish; the "Running n/N" counter already
                     carries that. -->
                <div v-if="isRunning && summary.total > 1" class="h-0.5 w-40 max-w-full bg-gray-100 dark:bg-gray-800 rounded overflow-hidden">
                  <div class="h-full bg-green-400 transition-all duration-300" :style="{ width: `${summary.progressPercent}%` }" />
                </div>
              </div>
            </div>
          </template>
        </UPopover>
      </div>
    </template>
  </span>
</template>

<script setup lang="ts">
// Compact resolved-eval control: a "Run eval" button + a chevron popover that
// mirrors the agents tree's EVALS node — one group per agent, listing that
// agent's SUITES with the cases underneath. Clicking a suite runs just that
// suite; the button at the top runs everything.
//
// Two details that are easy to get wrong and were wrong here before:
//
//   * The run goes to POST /tests/runs/batch, NOT /tests/runs. The latter only
//     creates `init` placeholder results and leaves execution to whoever opens
//     the run page's SSE stream (see TestRunService.create_run) — from here
//     nobody ever does, so the strip sat at "Running 0/1" until the 15-minute
//     watchdog errored it out. /runs/batch executes in the background itself.
//   * It runs explicit case_ids rather than a suite_id. A suite is a home, not
//     a scope: a suite-level run takes every case filed there, including ones
//     targeting a different agent. The ids shown in a row are the ids that run.
//
// When `buildId` is given the run is pinned to that suggestion/draft build, so
// it measures exactly that change in isolation.
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import Spinner from '~/components/Spinner.vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  instructionId: string
  buildId?: string | null
  /** Agents of the conversation this edit was made in. Only consulted for a
      GLOBAL instruction, which has no agents of its own — see the endpoint. */
  agentIds?: string[]
  /** The conversation to report the run's result back into when it finishes.
      Omitted outside a report (e.g. the console trace viewer), where there is
      no thread to wake. */
  reportId?: string | null
}>()

const { t } = useI18n()

const TERMINAL = ['pass', 'fail', 'error', 'stopped']

const data = ref<any>(null)
const loaded = ref(false)
const activeRun = ref<any>(null)
const results = ref<any[]>([])
const launchedCaseIds = ref<string[]>([])
const isStopping = ref(false)
let poll: any = null

const isRunning = computed(() => activeRun.value?.status === 'in_progress')
const allCaseIds = computed<string[]>(() => (data.value?.cases || []).map((c: any) => String(c.id)))
const hasShelf = computed(() => (data.value?.groups || []).some((g: any) => (g.suites || []).length))

const statusByCase = computed<Record<string, string>>(() => {
  const m: Record<string, string> = {}
  for (const r of results.value) m[String(r.case_id)] = r.status
  return m
})

const summary = computed(() => {
  const rs = results.value
  const passed = rs.filter((r: any) => r.status === 'pass').length
  const failed = rs.filter((r: any) => r.status === 'fail' || r.status === 'error').length
  const done = rs.filter((r: any) => TERMINAL.includes(r.status)).length
  // Total comes from what was launched, not from how many results exist yet —
  // results appear as the run creates them, so the denominator would otherwise
  // climb while the numerator does.
  const total = launchedCaseIds.value.length || rs.length
  return {
    total, passed, failed, done,
    inProgress: Math.max(0, total - done),
    progressPercent: total > 0 ? Math.round((done / total) * 100) : 0,
  }
})

const prettyStatus = computed(() => {
  const s = activeRun.value?.status
  if (s === 'in_progress') return t('agentsPage.evalStatusRunning')
  if (s === 'success') return t('agentsPage.evalStatusPassed')
  if (s === 'fail' || s === 'error') return t('agentsPage.evalStatusFailed')
  return s || '—'
})

function suiteRollup(s: any) {
  let passed = 0, failed = 0, done = 0
  for (const c of (s.cases || [])) {
    const st = statusByCase.value[String(c.id)]
    if (st === 'pass') { passed++; done++ }
    else if (st === 'fail' || st === 'error') { failed++; done++ }
    else if (st === 'stopped') done++
  }
  return { passed, failed, done }
}

function caseIcon(id: string) {
  const s = statusByCase.value[String(id)]
  if (s === 'pass') return 'i-heroicons-check-circle'
  if (s === 'fail' || s === 'error') return 'i-heroicons-x-circle'
  if (s === 'stopped') return 'i-heroicons-stop-circle'
  if (s === 'in_progress') return 'i-heroicons-arrow-path'
  return 'i-heroicons-clock'
}
function statusColor(id: string) {
  const s = statusByCase.value[String(id)]
  if (s === 'pass') return 'text-green-500'
  if (s === 'fail' || s === 'error') return 'text-red-500'
  if (s === 'stopped') return 'text-gray-500'
  if (s === 'in_progress') return 'text-blue-400 animate-spin'
  return 'text-gray-300 dark:text-gray-600'
}

async function load() {
  loaded.value = false
  try {
    const ids = (props.agentIds || []).filter(Boolean)
    const q = ids.length ? `?agent_ids=${encodeURIComponent(ids.join(','))}` : ''
    const res = await useMyFetch<any>(`/instructions/${props.instructionId}/resolved-evals${q}`, { method: 'GET' })
    data.value = res.data.value
  } catch (e) { data.value = null }
  loaded.value = true
}

async function runCases(caseIds: string[]) {
  const ids = (caseIds || []).filter(Boolean)
  if (!ids.length || isRunning.value) return
  results.value = []
  launchedCaseIds.value = ids
  try {
    const body: any = { case_ids: ids, trigger_reason: 'resolved_eval' }
    if (props.buildId) body.build_id = props.buildId
    // Name this conversation as the run's origin so it reports back here when
    // it finishes. Without it the result lives only in this component, and a
    // reload (or simply scrolling on with the chat) loses it — the transcript
    // is the durable place for the answer, not a popover's local state.
    if (props.reportId) {
      body.origin_report_id = props.reportId
      body.wake_on_finish = true
    }
    const res: any = await useMyFetch('/api/tests/runs/batch', { method: 'POST', body })
    const run = res?.data?.value
    if (!run?.id) { launchedCaseIds.value = []; return }
    activeRun.value = run
    await refreshRun()
    startPoll()
  } catch (e) {
    launchedCaseIds.value = []
  }
}

async function refreshRun() {
  const id = activeRun.value?.id
  if (!id) return
  try {
    const [runRes, resRes]: any[] = await Promise.all([
      useMyFetch<any>(`/api/tests/runs/${id}`),
      useMyFetch<any[]>(`/api/tests/runs/${id}/results`),
    ])
    if (runRes?.data?.value) activeRun.value = runRes.data.value
    results.value = resRes?.data?.value || []
  } catch (e) { /* keep the last good view; the poll will retry */ }
}

async function stopRun() {
  const id = activeRun.value?.id
  if (!id || isStopping.value) return
  isStopping.value = true
  try {
    // Stop the TestRun directly. There is no parent completion to sigkill the
    // way RunEvalTool does for an attached agent run — this run has no tool
    // call behind it, only the REST create.
    await useMyFetch(`/api/tests/runs/${id}/stop`, { method: 'POST' })
    await refreshRun()
  } catch (e) {
    /* the run page remains the fallback */
  } finally {
    isStopping.value = false
  }
}

function stopPoll() { if (poll) { clearInterval(poll); poll = null } }
function startPoll() {
  if (poll) return
  poll = setInterval(async () => {
    await refreshRun()
    if (activeRun.value && activeRun.value.status !== 'in_progress') stopPoll()
  }, 2000)
}

watch(() => [props.instructionId, props.buildId, (props.agentIds || []).join(',')], load)
onMounted(load)
onBeforeUnmount(stopPoll)
</script>
