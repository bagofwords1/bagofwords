<template>
  <div :class="embedded ? 'text-sm' : 'flex justify-center ps-2 md:ps-4 text-sm'">
    <div :class="embedded ? 'w-full' : 'w-full max-w-4xl px-4 ps-0 py-2'">
      <div :class="embedded ? 'space-y-4' : 'mt-6 space-y-4'">

        <!-- Back: embedded (inside the Agents explorer) emits so the panel
             swaps views in place; standalone navigates to backTo -->
        <button v-if="embedded" type="button" class="inline-flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200" @click="emit('back')">
          <Icon name="heroicons-arrow-left" class="w-3.5 h-3.5" />
          {{ $t('common.back') }}
        </button>
        <NuxtLink v-else :to="backTo || '/evals'" class="inline-flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200">
          <Icon name="heroicons-arrow-left" class="w-3.5 h-3.5" />
          {{ $t('evals.run.back') }}
        </NuxtLink>

        <!-- Header -->
        <div class="flex flex-wrap items-start gap-3">
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2 min-w-0">
              <h1 class="text-base font-semibold text-gray-900 dark:text-white truncate">
                {{ run?.title || $t('evals.runs.fallbackTitle') }}
              </h1>
              <span class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium rounded-full shrink-0" :class="runStatusClass(derivedRunStatus)">
                <Spinner v-if="derivedRunStatus === 'in_progress'" class="w-3 h-3" />
                {{ prettyStatus(derivedRunStatus) }}
              </span>
            </div>
            <div class="mt-1 text-xs text-gray-500 dark:text-gray-400 flex flex-wrap items-center gap-x-2 gap-y-0.5">
              <span v-if="suiteName">{{ suiteName }}</span>
              <span v-if="suiteName" class="text-gray-300 dark:text-gray-600">·</span>
              <span>{{ $t('evals.run.triggered', { when: timeAgo(run?.started_at), adverb: prettyTriggerAdverb(run?.trigger_reason) }) }}</span>
              <span class="text-gray-300 dark:text-gray-600">·</span>
              <span>{{ formatDuration(run?.started_at, run?.finished_at) }}</span>
              <template v-if="run?.build_id">
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <button type="button" class="inline-flex items-center gap-1 hover:text-gray-700 dark:hover:text-gray-200" @click="openBuild">
                  <Icon name="heroicons:cube" class="w-3 h-3" />
                  {{ $t('evals.run.build', { n: run?.build_number || '—' }) }}
                </button>
              </template>
            </div>
          </div>
          <UButton v-if="run?.status === 'in_progress'" color="red" size="2xs" variant="ghost" icon="i-heroicons-stop" @click="stopRun">{{ $t('evals.run.stop') }}</UButton>
        </div>

        <!-- Run summary -->
        <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] font-semibold">
          <span :class="passCount > 0 ? 'text-green-600 dark:text-green-500' : 'text-gray-400 dark:text-gray-500'">{{ $t('evals.run.pass', { n: passCount }) }}</span>
          <span :class="failCount > 0 ? 'text-red-500' : 'text-gray-400 dark:text-gray-500'">{{ $t('evals.run.fail', { n: failCount }) }}</span>
          <span :class="errorCount > 0 ? 'text-red-500' : 'text-gray-400 dark:text-gray-500'">{{ $t('evals.run.error', { n: errorCount }) }}</span>
        </div>

        <!-- Cases: one flat list, collapsed by default -->
        <div class="border border-gray-100 dark:border-gray-800 rounded-lg divide-y divide-gray-100 dark:divide-gray-800">
          <div v-for="row in caseRows" :key="row.result.id">
            <!-- Collapsed row -->
            <button type="button" class="w-full flex items-center gap-2 px-3 py-2.5 text-start hover:bg-gray-50/70 dark:hover:bg-gray-800/40" @click="toggleRow(row.result.id)">
              <span class="shrink-0 w-4 flex justify-center">
                <Spinner v-if="row.result.status === 'in_progress'" class="w-3.5 h-3.5 text-gray-400" />
                <Icon v-else-if="row.result.status === 'pass'" name="heroicons-check" class="w-4 h-4 text-green-600" />
                <Icon v-else-if="row.result.status === 'fail'" name="heroicons-x-mark" class="w-4 h-4 text-red-500" />
                <Icon v-else-if="row.result.status === 'error'" name="heroicons-exclamation-triangle" class="w-4 h-4 text-red-500" :title="(row.result as any).failure_reason || ''" />
                <Icon v-else name="heroicons-clock" class="w-4 h-4 text-gray-300 dark:text-gray-600" />
              </span>
              <span class="text-sm text-gray-900 dark:text-white truncate">{{ row.case?.name || '—' }}</span>
              <span class="ms-auto flex items-center gap-3 shrink-0 text-[11px] text-gray-400 dark:text-gray-500">
                <span v-if="assertionCount(row)">{{ passedAssertions(row) }}/{{ assertionCount(row) }}</span>
                <span>{{ caseDuration(row) }}</span>
                <Icon name="heroicons-chevron-down" :class="['w-3.5 h-3.5 transition-transform', isRowExpanded(row.result.id) ? 'rotate-180' : '']" />
              </span>
            </button>

            <!-- Expanded: conversation | checks, side by side -->
            <div v-if="isRowExpanded(row.result.id)" class="pb-1">
              <div class="grid grid-cols-1 md:grid-cols-2 md:divide-x md:divide-gray-100 dark:md:divide-gray-800 border-t border-gray-100 dark:border-gray-800">

              <!-- Conversation (report-style, compact) -->
              <div class="space-y-3 px-4 py-3 min-w-0">
                <template v-for="(entry, i) in timelineFor(row)" :key="i">
                  <!-- User turn -->
                  <div v-if="entry.kind === 'user'" class="flex justify-end">
                    <div class="max-w-[85%] rounded-xl px-3 py-1.5 bg-gray-50 dark:bg-gray-800 text-xs text-gray-900 dark:text-white whitespace-pre-wrap break-words" dir="auto">{{ entry.text }}</div>
                  </div>
                  <!-- Thinking -->
                  <div v-else-if="entry.kind === 'thinking'" class="text-[11px] text-gray-400 dark:text-gray-500 italic leading-relaxed" :class="expandedThinking[`${row.result.id}:${i}`] ? '' : 'line-clamp-2'" role="button" @click="expandedThinking[`${row.result.id}:${i}`] = !expandedThinking[`${row.result.id}:${i}`]" dir="auto">{{ entry.text }}</div>
                  <!-- Tool call -->
                  <div v-else-if="entry.kind === 'tool'" class="flex items-start gap-1.5 text-[11px] text-gray-500 dark:text-gray-400">
                    <Icon :name="entry.status === 'error' ? 'heroicons-exclamation-circle' : 'heroicons-wrench-screwdriver'" :class="['w-3 h-3 mt-0.5 shrink-0', entry.status === 'error' ? 'text-red-400' : 'text-gray-400']" />
                    <span class="min-w-0">
                      <span class="font-medium text-gray-600 dark:text-gray-300">{{ entry.name }}</span>
                      <span v-if="entry.summary" class="text-gray-400 dark:text-gray-500"> — {{ entry.summary }}</span>
                    </span>
                  </div>
                  <!-- Error -->
                  <div v-else-if="entry.kind === 'error'" class="flex items-start gap-1.5 text-[11px] text-red-500">
                    <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 mt-0.5 shrink-0" />
                    <span class="min-w-0 whitespace-pre-wrap break-words">{{ entry.text }}</span>
                  </div>
                  <!-- Assistant message -->
                  <div v-else class="eval-md text-xs text-gray-800 dark:text-gray-200" dir="auto">
                    <MarkdownRender :content="entry.text" :final="true" :typewriter="false" :render-code-blocks-as-pre="true" class="markdown-content" />
                  </div>
                </template>
                <div v-if="timelineFor(row).length === 0" class="text-xs text-gray-400 dark:text-gray-500">—</div>
                <div v-if="(row.result as any).failure_reason" class="text-[11px] text-red-500">{{ (row.result as any).failure_reason }}</div>
              </div>

              <!-- Checks -->
              <div class="px-4 py-3 min-w-0">
                <div v-if="displayRules(row).length" class="space-y-2">
                  <div v-for="it in displayRules(row)" :key="it.originalIdx" class="flex items-start gap-2 text-xs">
                    <span class="shrink-0 w-4 flex justify-center mt-0.5">
                      <Spinner v-if="ruleStatus(row, it.originalIdx) === 'pending'" class="w-3 h-3 text-gray-400" />
                      <Icon v-else-if="ruleStatus(row, it.originalIdx) === 'pass'" name="heroicons-check" class="w-3.5 h-3.5 text-green-600" />
                      <Icon v-else-if="ruleStatus(row, it.originalIdx) === 'skipped'" name="heroicons-minus-circle" class="w-3.5 h-3.5 text-gray-300 dark:text-gray-600" />
                      <Icon v-else name="heroicons-x-mark" class="w-3.5 h-3.5 text-red-500" />
                    </span>
                    <div class="min-w-0 flex-1">
                      <div class="text-gray-800 dark:text-gray-200">
                        <span class="text-gray-400 dark:text-gray-500">{{ categoryName(ruleCategoryKey(it.rule)) }} · </span>{{ ruleSummaryText(it.rule) }}
                      </div>
                      <div v-if="ruleSecondaryText(row, it)" class="mt-0.5 text-[11px] text-gray-400 dark:text-gray-500 leading-relaxed">{{ ruleSecondaryText(row, it) }}</div>
                    </div>
                  </div>
                </div>
              </div>

              </div>
              <!-- Meta footer -->
              <div class="px-4 py-2 border-t border-gray-100 dark:border-gray-800 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-gray-400 dark:text-gray-500">
                <span class="inline-flex items-center gap-1.5">
                  <LLMProviderIcon :provider="modelProviderType(row.case?.prompt_json?.model_id, row.case)" :icon="true" class="w-3.5 h-3.5" />
                  {{ modelDisplayName(row.case?.prompt_json?.model_id, row.case) }}
                </span>
                <span v-for="dsId in (row.case?.data_source_ids_json || [])" :key="dsId" class="inline-flex items-center gap-1" v-show="dataSourceById[dsId]">
                  <DataSourceIcon v-if="dataSourceById[dsId]" :type="dataSourceById[dsId].type" :icon="dataSourceById[dsId].icon" class="h-3" />
                  {{ dataSourceById[dsId]?.name }}
                </span>
                <NuxtLink v-if="row.result.report_id" :to="`/reports/${row.result.report_id}`" target="_blank" class="ms-auto inline-flex items-center gap-1 text-blue-500 hover:underline">
                  {{ $t('evals.run.openReport') }}
                  <Icon name="heroicons-arrow-top-right-on-square" class="w-3 h-3" />
                </NuxtLink>
              </div>
            </div>
          </div>
          <div v-if="caseRows.length === 0" class="px-3 py-6 text-center text-xs text-gray-400 dark:text-gray-500">—</div>
        </div>
      </div>
    </div>
    <BuildExplorerModal v-model="showBuildModal" :build-id="buildModalId" />
  </div>
</template>

<script setup lang="ts">
import LLMProviderIcon from '~/components/LLMProviderIcon.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import Spinner from '~/components/Spinner.vue'
import BuildExplorerModal from '~/components/instructions/BuildExplorerModal.vue'
import { MarkdownRender } from 'markstream-vue'

const { t } = useI18n()

const props = defineProps<{ runId: string; backTo?: string; embedded?: boolean }>()
const emit = defineEmits<{ (e: 'back'): void }>()
const runId = computed(() => String(props.runId || ''))


type TestRun = {
  id: string
  suite_ids?: string
  trigger_reason?: string
  status: 'in_progress' | 'success' | 'error' | 'stopped'
  started_at?: string
  finished_at?: string
  title?: string
  build_id?: string
  build_number?: number
}

type RuleResult = { ok: boolean, status?: string, message?: string, actual?: any, evidence?: any }
type ResultJson = { totals?: any, rule_results?: RuleResult[], spec?: { rules?: any[] } }
type TestResult = { id: string, run_id: string, case_id: string, status: string, result_json?: ResultJson, report_id?: string }
type TestCase = { id: string, name: string, prompt_json: any, expectations_json: { rules: any[] }, data_source_ids_json?: string[] }
type CaseRow = { result: TestResult, case: TestCase }

// Conversation entry, rebuilt from the run's completions — same data the
// report page renders, in a compact form.
type TimelineEntry =
  | { kind: 'user', text: string }
  | { kind: 'thinking', text: string }
  | { kind: 'message', text: string }
  | { kind: 'tool', name: string, summary: string, status?: string }
  | { kind: 'error', text: string }

const run = ref<TestRun | null>(null)
const results = ref<TestResult[]>([])
const suiteName = ref<string>('')
const caseRows = ref<CaseRow[]>([])
const openRows = ref<Record<string, boolean>>({})
const expandedThinking = reactive<Record<string, boolean>>({})
const models = ref<any[]>([])
const modelById = computed<Record<string, any>>(() => Object.fromEntries((models.value || []).map((m: any) => [m.model_id || m.id, m])))
const dataSources = ref<any[]>([])
const dataSourceById = reactive<Record<string, any>>({})
const timelines = reactive<Record<string, TimelineEntry[]>>({})

const showBuildModal = ref(false)
const buildModalId = ref<string | undefined>(undefined)
function openBuild() {
  if (!run.value?.build_id) return
  buildModalId.value = run.value.build_id
  showBuildModal.value = true
}

const suiteId = computed<string>(() => {
  const raw: any = (run.value as any)?.suite_id ?? (run.value as any)?.suite_ids ?? ''
  if (!raw) return ''
  return String(raw).split(',')[0].trim()
})

const isRowExpanded = (resultId: string) => !!openRows.value[resultId]
const toggleRow = (resultId: string) => { openRows.value[resultId] = !openRows.value[resultId] }

// ---- Conversation timelines --------------------------------------------

function timelineFromCompletions(comps: any[], caseObj?: TestCase): TimelineEntry[] {
  const out: TimelineEntry[] = []
  const ordered = [...(comps || [])].sort((a, b) => new Date(a?.created_at || 0).getTime() - new Date(b?.created_at || 0).getTime())
  for (const c of ordered) {
    if ((c?.role || '') === 'user') {
      const text = c?.prompt?.content || ''
      if (text) out.push({ kind: 'user', text })
      continue
    }
    if ((c?.role || '') !== 'system') continue
    const blocks = Array.isArray(c?.completion_blocks) ? c.completion_blocks : []
    let emitted = 0
    for (const b of blocks) {
      const reasoning = b?.plan_decision?.reasoning || b?.reasoning || ''
      if (reasoning) { out.push({ kind: 'thinking', text: String(reasoning) }); emitted++ }
      const te = b?.tool_execution
      if (te?.tool_name) { out.push({ kind: 'tool', name: String(te.tool_name), summary: String(te.result_summary || ''), status: te.status }); emitted++ }
      const content = b?.content || b?.plan_decision?.final_answer || b?.plan_decision?.assistant || ''
      if (b?.status === 'error') {
        // Surface the failure itself — an errored block used to vanish,
        // leaving a run that "failed and i have no idea why".
        out.push({ kind: 'error', text: String(content || b?.title || 'The agent errored') })
        emitted++
      } else if (content) {
        out.push({ kind: 'message', text: String(content) })
        emitted++
      }
    }
    if ((c?.status || '') === 'error' && emitted === 0) {
      out.push({ kind: 'error', text: 'The agent run errored before producing output' })
    }
  }
  // Nothing ran yet — show the case prompt so the row isn't empty.
  if (!out.length && caseObj?.prompt_json?.content) out.push({ kind: 'user', text: caseObj.prompt_json.content })
  return out
}

function timelineFor(row: CaseRow): TimelineEntry[] {
  const tl = timelines[String(row.result.id)]
  if (tl && tl.length) return tl
  const text = row.case?.prompt_json?.content || ''
  return text ? [{ kind: 'user', text }] : []
}

async function refreshTimelines() {
  try {
    const statusRes: any = await useMyFetch(`/api/tests/runs/${runId.value}/status?limit=20`)
    const payload = (statusRes?.data?.value || {}) as any
    const items = Array.isArray(payload?.results) ? payload.results : []
    for (const it of items) {
      const rid = String(it?.result?.id || '')
      if (!rid) continue
      timelines[rid] = timelineFromCompletions(it?.completions || [])
    }
  } catch {}
}

// ---- Load ---------------------------------------------------------------

const load = async () => {
  try {
    const [runRes, resRes, modelsRes, dsRes] = await Promise.all([
      useMyFetch<TestRun>(`/api/tests/runs/${runId.value}`),
      useMyFetch<TestResult[]>(`/api/tests/runs/${runId.value}/results`),
      useMyFetch<any[]>(`/api/llm/models?is_enabled=true`),
      useMyFetch<any[]>(`/data_sources/active`),
    ])
    run.value = runRes.data.value as any
    results.value = (resRes.data.value as any[]) || []
    models.value = (modelsRes.data.value as any[]) || []
    dataSources.value = (dsRes.data.value as any[]) || []
    for (const ds of dataSources.value) dataSourceById[String(ds.id)] = ds

    const sid = suiteId.value
    if (sid) {
      const suiteRes: any = await useMyFetch(`/api/tests/suites/${sid}`)
      suiteName.value = suiteRes?.data?.value?.name || ''
    }
    const caseFetches = results.value.map(r => useMyFetch<TestCase>(`/api/tests/cases/${r.case_id}`))
    const caseResponses = await Promise.all(caseFetches)
    const casesById: Record<string, TestCase> = {}
    for (const cr of caseResponses) {
      const c = cr.data.value as any
      if (c?.id) casesById[c.id] = c
    }
    caseRows.value = results.value.map(r => ({ result: r, case: casesById[r.case_id] }))
    // A single-case run has nothing to scan — open it right away.
    if (results.value.length === 1) openRows.value[String(results.value[0].id)] = true
    await refreshTimelines()
  } catch (e) {
    console.error('Failed to load run', e)
  }
}

// ---- Status helpers -----------------------------------------------------

const runStatusClass = (status?: string) => {
  if (status === 'success' || status === 'pass') return 'bg-green-100 text-green-800'
  if (status === 'error' || status === 'fail') return 'bg-red-100 text-red-800'
  return 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300'
}

const prettyStatus = (status?: string) => {
  if (!status) return '—'
  if (status === 'in_progress') return t('evals.run.statusInProgress')
  if (status === 'success') return t('evals.run.statusSuccess')
  if (status === 'fail') return t('evals.run.statusFailed')
  if (status === 'error') return t('evals.run.statusError')
  if (status === 'stopped') return t('evals.run.statusStopped')
  return status.replace('_', ' ')
}

const passCount = computed(() => results.value.filter(r => r.status === 'pass').length)
const failCount = computed(() => results.value.filter(r => r.status === 'fail').length)
const errorCount = computed(() => results.value.filter(r => r.status === 'error').length)

const derivedRunStatus = computed<'in_progress' | 'success' | 'fail' | 'error' | 'stopped'>(() => {
  try {
    if (run.value?.status === 'stopped') return 'stopped'
    const list = results.value || []
    if (list.some(r => r.status === 'in_progress')) return 'in_progress'
    if (list.some(r => r.status === 'error')) return 'error'
    if (list.some(r => r.status === 'fail')) return 'fail'
    if (list.length > 0 && list.every(r => r.status === 'pass')) return 'success'
    return (run.value?.status as any) || 'in_progress'
  } catch {
    return (run.value?.status as any) || 'in_progress'
  }
})

// ---- Time formatting ----------------------------------------------------

const timeAgo = (iso?: string | null) => {
  if (!iso) return '—'
  try {
    const diffSec = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000))
    if (diffSec < 60) return t('evals.run.agoSeconds', { n: diffSec })
    const mins = Math.floor(diffSec / 60)
    if (mins < 60) return t('evals.run.agoMinutes', { n: mins })
    const hours = Math.floor(mins / 60)
    if (hours < 24) return t('evals.run.agoHours', { n: hours })
    return t('evals.run.agoDays', { n: Math.floor(hours / 24) })
  } catch {
    return '—'
  }
}

const prettyTriggerAdverb = (reason?: string | null) => {
  const r = String(reason || 'manual').toLowerCase()
  if (r === 'manual') return t('evals.run.triggerManually')
  return r
}

const formatDuration = (start?: string | null, end?: string | null) => {
  if (!start) return '—'
  const s = new Date(start).getTime()
  const e = end ? new Date(end).getTime() : Date.now()
  const secs = Math.round(Math.max(0, e - s) / 1000)
  if (secs < 60) return `${secs}s`
  return `${Math.floor(secs / 60)}m ${secs % 60}s`
}

const caseDuration = (row: CaseRow) => {
  const ms = row.result.result_json?.totals && typeof row.result.result_json.totals.duration_ms === 'number'
    ? Number(row.result.result_json.totals.duration_ms)
    : null
  if (typeof ms !== 'number') return '—'
  if (ms < 1000) return `${ms}ms`
  const secs = Math.round(ms / 1000)
  if (secs < 60) return `${secs}s`
  return `${Math.floor(secs / 60)}m ${secs % 60}s`
}

// ---- Rules --------------------------------------------------------------

const specRulesForRow = (row: CaseRow): any[] => {
  const specRules = row.result.result_json?.spec?.rules
  if (Array.isArray(specRules)) return specRules
  return row.case?.expectations_json?.rules || []
}

const displayRules = (row: CaseRow): Array<{ rule: any, originalIdx: number }> => {
  const rules = specRulesForRow(row)
  const out: Array<{ rule: any, originalIdx: number }> = []
  for (let i = 0; i < rules.length; i++) {
    const r = rules[i]
    // Judge model_id is configuration, not an assertion.
    if (r?.target?.category === 'judge' && r?.target?.field === 'model_id') continue
    out.push({ rule: r, originalIdx: i })
  }
  return out
}

const assertionCount = (row: CaseRow) => displayRules(row).length

const passedAssertions = (row: CaseRow): number => {
  const rr = row.result.result_json?.rule_results || []
  if (!Array.isArray(rr)) return 0
  let cnt = 0
  for (const it of displayRules(row)) {
    const entry = rr[it.originalIdx]
    if (entry && entry.ok === true) cnt++
  }
  return cnt
}

const CATEGORY_LABELS = computed<Record<string, string>>(() => ({
  'tool:create_data': t('evals.category.createData'),
  'tool:clarify': t('evals.category.clarify'),
  'tool:describe_table': t('evals.category.describeTable'),
  'metadata': t('evals.category.metadata'),
  'completion': t('evals.category.completion'),
  'judge': t('evals.category.judge'),
}))
const humanize = (s?: string) => String(s || '').replace(/_/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase())
const categoryName = (cat?: string) => {
  const c = String(cat || '')
  if (!c) return ''
  if (CATEGORY_LABELS.value[c]) return CATEGORY_LABELS.value[c]
  if (c.startsWith('tool:')) return humanize(c.split(':')[1] || '')
  return humanize(c)
}
const ruleCategoryKey = (rule: any): string => {
  const legacy = String(rule?.target?.category || '')
  if (legacy) return legacy
  const type = String(rule?.type || '')
  if (type === 'judge') return 'judge'
  if (type === 'tool.calls') return `tool:${rule?.tool || ''}`
  if (type === 'ordering') return 'ordering'
  if (type === 'phase') return 'phase'
  return type
}
const isJudgeRule = (rule: any): boolean => rule?.type === 'judge' || String(rule?.target?.category || '') === 'judge'

const opLabel = (op?: string) => {
  switch (op) {
    case 'text.contains': return t('evals.op.textContains')
    case 'text.not_contains': return t('evals.op.textNotContains')
    case 'text.equals': return t('evals.op.textEquals')
    case 'text.regex': return t('evals.op.textRegex')
    case 'number.cmp': return t('evals.op.numberCmp')
    case 'length.cmp': return t('evals.op.lengthCmp')
    case 'list.contains': return t('evals.op.listContains')
    case 'list.contains_any': return t('evals.op.listContainsAny')
    case 'list.contains_all': return t('evals.op.listContainsAll')
    default: return String(op || '')
  }
}
const cmpSymbol = (op?: string) => {
  switch (op) {
    case 'gt': return '>'
    case 'gte': return '≥'
    case 'lt': return '<'
    case 'lte': return '≤'
    case 'eq': return '='
    case 'ne': return '≠'
    default: return String(op || '')
  }
}

const quote = (s: string) => `"${s}"`
const joinedQuoted = (arr: any[]) => quote(arr.map((v) => String(v)).join('; '))
const ruleSummaryText = (rule: any): string => {
  try {
    if (isJudgeRule(rule)) {
      const val = rule?.prompt ?? rule?.matcher?.value ?? rule?.target?.value
      return typeof val === 'string' ? val : String(val ?? 'Prompt')
    }
    if (rule?.type === 'tool.calls') {
      const tool = rule?.tool || 'tool'
      const bounds: string[] = []
      if (typeof rule?.min_calls === 'number' && rule.min_calls > 0) bounds.push(`≥ ${rule.min_calls}`)
      if (typeof rule?.max_calls === 'number' && rule.max_calls != null) bounds.push(`≤ ${rule.max_calls}`)
      return bounds.length ? `${tool} called ${bounds.join(', ')}` : `${tool} called`
    }
    if (rule?.type === 'ordering') {
      const seq = Array.isArray(rule?.sequence) ? rule.sequence.map((s: any) => s?.tool_or_bind || '?') : []
      return seq.length ? `Order: ${seq.join(' → ')}` : 'Ordering'
    }
    if (rule?.type === 'phase') {
      return `Phase '${rule?.phase}' ${rule?.occurred === false ? 'did not run' : 'ran'}`
    }
    const field = humanize(rule?.target?.field || '')
    const op = opLabel(rule?.matcher?.type)
    const m = rule?.matcher || {}
    if (m?.type === 'list.contains_any' || m?.type === 'list.contains_all') {
      const vals = Array.isArray(m?.values) ? m.values : []
      return `${field} ${op} ${joinedQuoted(vals)}`
    }
    if (m?.type === 'text.regex') return `${field} ${op} /${String(m?.pattern || '')}/`
    if (m?.type === 'number.cmp' || m?.type === 'length.cmp') return `${field} ${op} ${cmpSymbol(m?.op)} ${m?.value}`
    return `${field} ${op} ${quote(String(m?.value ?? ''))}`
  } catch {
    return '—'
  }
}

const ruleResultAt = (row: CaseRow, idx: number) => {
  const rr = row.result.result_json?.rule_results || []
  if (!Array.isArray(rr) || idx < 0 || idx >= rr.length) return null
  return rr[idx] || null
}
const RESULT_TERMINAL = ['pass', 'fail', 'error', 'stopped']
const ruleStatus = (row: CaseRow, idx: number): 'pending' | 'pass' | 'fail' | 'skipped' => {
  const rr = ruleResultAt(row, idx)
  if (!rr || typeof rr.ok !== 'boolean') {
    // No persisted verdict. Pending only while the case can still produce
    // one — a stopped/errored case never evaluated its rules, and showing
    // a spinner forever there read as "still judging".
    return RESULT_TERMINAL.includes(String(row.result.status || '')) ? 'skipped' : 'pending'
  }
  if (rr.status === 'skipped') return 'skipped'
  return rr.ok ? 'pass' : 'fail'
}

// One compact secondary line per rule: judge reasoning, failure message, or
// the observed value — whichever says the most, without repeating itself.
const ruleSecondaryText = (row: CaseRow, it: { rule: any, originalIdx: number }): string => {
  const rr: any = ruleResultAt(row, it.originalIdx)
  if (!rr) return ''
  const reasoning = String(rr?.evidence?.reasoning || '')
  const message = String(rr?.message || '')
  if (isJudgeRule(it.rule)) return reasoning || message
  const parts: string[] = []
  const actual = rr?.actual
  if (actual != null && ruleStatus(row, it.originalIdx) !== 'pending') {
    const text = typeof actual === 'string' ? actual : Array.isArray(actual) ? actual.map(String).join('; ') : JSON.stringify(actual)
    if (text !== '' && text !== 'true') parts.push(`${t('evals.run.actual')} ${text}`)
  }
  if (message && message !== reasoning) parts.push(message)
  return parts.join(' · ')
}

// ---- Model / provider ---------------------------------------------------

const orgDefaultModel = computed<any | null>(() => (models.value || []).find((m: any) => m?.is_default) || null)
const resolveModel = (modelId?: string, caseObj?: TestCase): any | null => {
  const m = modelById.value[String(modelId || '')]
  if (m) return m
  const ms: any = (caseObj as any)?.model_summary
  if (ms) return ms
  if (!modelId) return orgDefaultModel.value
  return null
}
const modelProviderType = (modelId?: string, caseObj?: TestCase) => {
  const m = resolveModel(modelId, caseObj)
  return m?.provider?.provider_type || m?.provider_type || 'default'
}
const modelDisplayName = (modelId?: string, caseObj?: TestCase) => {
  const m = resolveModel(modelId, caseObj)
  return m?.name || m?.model_id || modelId || t('evals.run.defaultModel')
}

// ---- Stop / refresh -----------------------------------------------------

const stopRun = async () => {
  try {
    if (!run.value?.id || run.value.status !== 'in_progress') return
    await useMyFetch(`/api/tests/runs/${run.value.id}/stop`, { method: 'POST' })
    await load()
  } catch (e) {
    console.error('Failed to stop run', e)
  }
}

const refreshTimer = ref<any>(null)
const timelineTimer = ref<any>(null)
const isTerminalStatus = (s?: string | null) => ['pass', 'fail', 'error', 'success', 'stopped'].includes(String(s || ''))

const reloadRun = async () => {
  try {
    const runRes = await useMyFetch<TestRun>(`/api/tests/runs/${runId.value}`)
    run.value = runRes.data.value as any
  } catch {}
}
const reloadResults = async () => {
  try {
    const resRes = await useMyFetch<TestResult[]>(`/api/tests/runs/${runId.value}/results`)
    const newResults = (resRes.data.value as any[]) || []
    results.value = newResults
    const byCase: Record<string, TestCase> = Object.fromEntries(caseRows.value.filter(r => r.case).map(r => [r.case.id, r.case]))
    caseRows.value = newResults.map(r => ({ result: r, case: byCase[r.case_id] }))
  } catch (e) {
    console.error('Failed to reload results', e)
  }
}
const scheduleResultsRefresh = (withRun: boolean = false, delayMs: number = 400) => {
  if (refreshTimer.value) clearTimeout(refreshTimer.value)
  refreshTimer.value = setTimeout(async () => {
    try {
      if (withRun) await reloadRun()
      await reloadResults()
      await refreshTimelines()
    } finally {
      refreshTimer.value = null
    }
  }, Math.max(0, delayMs || 0))
}
// Debounced conversation refresh while agents stream.
const scheduleTimelineRefresh = (delayMs: number = 900) => {
  if (timelineTimer.value) return
  timelineTimer.value = setTimeout(async () => {
    try { await refreshTimelines() } finally { timelineTimer.value = null }
  }, delayMs)
}

onMounted(load)

// Run-level streaming: statuses arrive as events; conversation content is
// re-pulled (debounced) from /status — same source the report page renders.
onMounted(async () => {
  try {
    setTimeout(async () => {
      try {
        const raw: any = await useMyFetch(`/tests/runs/${runId.value}/stream`, { method: 'POST', stream: true } as any)
        const res: Response = (raw?.data?.value ?? raw?.data) as unknown as Response
        const reader = res?.body?.getReader?.()
        if (!reader) return
        const decoder = new TextDecoder()
        let buffer = ''
        const processChunk = (text: string) => {
          buffer += text
          let idx
          while ((idx = buffer.indexOf('\n\n')) !== -1) {
            const rawMsg = buffer.slice(0, idx)
            buffer = buffer.slice(idx + 2)
            const lines = rawMsg.split('\n')
            let eventName = 'message'
            let data = ''
            for (const line of lines) {
              if (line.startsWith('event:')) eventName = line.slice(6).trim()
              else if (line.startsWith('data:')) data += line.slice(5).trim()
            }
            if (!data) continue
            try {
              const parsed = JSON.parse(data)
              const payload = (parsed && typeof parsed === 'object' && 'data' in parsed) ? (parsed as any).data : parsed
              if (eventName === 'run.started') {
                if (run.value) run.value.status = 'in_progress'
              } else if (eventName === 'result.update') {
                const rid = String((payload as any).result_id || '')
                const i = results.value.findIndex(r => String(r.id) === rid)
                if (i >= 0) {
                  const copy = { ...results.value[i] }
                  if ((payload as any).status) (copy as any).status = (payload as any).status
                  if ((payload as any).result_json) (copy as any).result_json = (payload as any).result_json
                  const tmp = [...results.value]
                  tmp[i] = copy
                  results.value = tmp
                }
                if (isTerminalStatus((payload as any)?.status)) scheduleResultsRefresh(false)
                else scheduleTimelineRefresh()
              } else if (eventName === 'run.finished') {
                if (run.value && (payload as any)?.status) (run.value as any).status = (payload as any).status
                scheduleResultsRefresh(true)
              } else if (eventName === 'completion.finished' || eventName === 'completion.error') {
                scheduleResultsRefresh(false, 600)
              } else {
                // block.*, decision.*, tool.* — content changed somewhere.
                scheduleTimelineRefresh()
              }
            } catch {}
          }
        }
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          processChunk(decoder.decode(value, { stream: true }))
        }
        if (buffer.length) processChunk(buffer)
        scheduleResultsRefresh(true)
      } catch (e: any) {
        // Navigating away aborts the stream fetch — that's not a failure.
        const msg = String(e?.message || e)
        if (!/abort|network error/i.test(msg)) console.error('Run stream failed', e)
      }
    }, 100)
  } catch {}
})

onBeforeUnmount(() => {
  if (refreshTimer.value) clearTimeout(refreshTimer.value)
  if (timelineTimer.value) clearTimeout(timelineTimer.value)
})
</script>

<style scoped>
.eval-md :deep(.markdown-content) {
  font-size: 12px;
  line-height: 1.55;
}
.eval-md :deep(.markdown-content p) {
  margin: 0.35em 0;
}
.eval-md :deep(.markdown-content table) {
  font-size: 11px;
}
.eval-md :deep(.markdown-content pre) {
  font-size: 11px;
}
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  cursor: pointer;
}
</style>
