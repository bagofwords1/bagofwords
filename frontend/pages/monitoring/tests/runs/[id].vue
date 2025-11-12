<template>
  <div class="mt-6">
    <!-- Run header -->
    <div class="bg-white border border-gray-200 rounded-xl p-5 mb-6">
      <div class="flex flex-wrap items-center gap-3">
        <div class="min-w-0">
          <div class="text-lg font-semibold text-gray-900 truncate">
            {{ run?.title || 'Test Run' }}
          </div>
          <div class="text-xs text-gray-500 truncate">
            Suite · {{ suiteName || run?.suite_id || '—' }}
          </div>
        </div>
        <span class="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-full" :class="runStatusClass(run?.status)">
          <Spinner v-if="run?.status === 'in_progress'" class="w-3 h-3" />
          {{ prettyStatus(run?.status) }}
        </span>
        <div class="text-xs text-gray-500">Trigger: <span class="capitalize">{{ run?.trigger_reason || 'manual' }}</span></div>
        <div class="text-xs text-gray-500">Started: {{ formatDate(run?.started_at) }}</div>
        <div class="text-xs text-gray-500">Finished: {{ formatDate(run?.finished_at) }}</div>
        <div class="text-xs text-gray-500 ml-auto flex items-center gap-2">
          <span>Duration: {{ formatDuration(run?.started_at, run?.finished_at) }}</span>
          <UButton color="red" size="xs" variant="soft" icon="i-heroicons-stop" @click="stopRun" :disabled="run?.status !== 'in_progress'">Stop</UButton>
        </div>
      </div>
      <div class="mt-3 text-xs text-gray-600 flex flex-wrap items-center gap-2">
        <span class="inline-flex items-center px-2 py-1 rounded-full border bg-slate-50 text-slate-700 border-slate-200">Cases: {{ results.length }}</span>
        <span class="inline-flex items-center px-2 py-1 rounded-full border bg-green-50 text-green-700 border-green-200">Pass: {{ passCount }}</span>
        <span class="inline-flex items-center px-2 py-1 rounded-full border bg-red-50 text-red-700 border-red-200">Fail: {{ failCount }}</span>
        <span class="inline-flex items-center px-2 py-1 rounded-full border bg-gray-50 text-gray-700 border-gray-200">Error: {{ errorCount }}</span>
      </div>
    </div>

    <!-- Each result (case) - collapsed list with expandable single-container split -->
    <div class="space-y-4">
      <div v-for="row in caseRows" :key="row.result.id" class="bg-white border border-gray-200 rounded-lg shadow-sm overflow-hidden">
        <!-- Collapsed header -->
        <button type="button" class="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-gray-50" @click="toggleRow(row.result.id)">
          <div class="flex items-center gap-3 min-w-0">
            <span class="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium rounded-full" :class="runStatusClass(row.result.status)">
              <Spinner v-if="row.result.status === 'in_progress'" class="w-3 h-3" />
              {{ prettyStatus(row.result.status) }}
            </span>
            <span class="text-sm font-medium text-gray-900 truncate">{{ row.case.name }}</span>
            <span class="text-xs text-gray-500">| {{ assertionCount(row) }} assertions</span>
          </div>
          <div class="flex items-center gap-2">
            <span class="text-xs text-gray-500">{{ caseDuration(row) }}</span>
            <svg :class="['w-4 h-4 text-gray-500 transition-transform', isRowExpanded(row.result.id) ? 'rotate-180' : '']" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
              <path fill-rule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 10.94l3.71-3.71a.75.75 0 111.06 1.06l-4.24 4.24a.75.75 0 01-1.06 0L5.21 8.29a.75.75 0 01.02-1.08z" clip-rule="evenodd" />
            </svg>
          </div>
        </button>
        <!-- Expanded content -->
        <div v-if="isRowExpanded(row.result.id)" class="border-t border-gray-200">
          <div class="grid grid-cols-1 md:grid-cols-2 md:divide-x md:divide-gray-200">
            <!-- Left: Prompt and metadata -->
            <div class="p-4 space-y-3 text-xs text-gray-800">
              <!-- Logs -->
              <div class="flex items-center justify-between">
                <div class="text-[11px] text-gray-500">Logs</div>
                <span class="inline-flex items-center gap-1 px-2 py-1 text-[10px] font-medium rounded-full" :class="runStatusClass(row.result.status)">
                  <Spinner v-if="row.result.status === 'in_progress'" class="w-3 h-3" />
                  {{ prettyStatus(row.result.status) }}
                </span>
              </div>
              <div class="bg-gray-50 rounded p-3 text-xs">
                <div class="space-y-2">
                  <div v-if="(getLogs(row.result.id) || []).length === 0" class="text-gray-500">—</div>
                  <div v-for="(e, mi) in getLogs(row.result.id)" :key="mi" class="flex items-start gap-2">
                    <span class="uppercase text-[10px] font-medium text-gray-500 w-32 shrink-0 truncate">{{ e.label }}</span>
                    <div class="text-gray-800 whitespace-pre-wrap break-words flex-1">{{ e.text }}</div>
                  </div>
                </div>
              </div>
              <div class="flex items-center justify-between">
                <div class="text-[11px] text-gray-500">Prompt</div>
                <span class="inline-flex items-center gap-1 px-2 py-1 text-[10px] font-medium rounded-full" :class="runStatusClass(row.result.status)">
                  <Spinner v-if="row.result.status === 'in_progress'" class="w-3 h-3" />
                  {{ prettyStatus(row.result.status) }}
                </span>
              </div>
              <pre class="whitespace-pre-wrap break-words bg-gray-50 rounded p-3 text-xs">{{ row.case.prompt_json?.content || '—' }}</pre>
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <!-- Model -->
                <div class="min-w-0">
                  <div class="text-[11px] text-gray-500 mb-1">Model</div>
                  <div class="flex items-center gap-2">
                    <LLMProviderIcon :provider="modelProviderType(row.case.prompt_json?.model_id)" :icon="true" class="w-4 h-4" />
                    <div class="min-w-0">
                      <div class="text-xs text-gray-900 truncate">{{ modelIdText(row.case.prompt_json?.model_id) }}</div>
                      <div class="text-[10px] text-gray-500 truncate">{{ modelProviderName(row.case.prompt_json?.model_id) }}</div>
                    </div>
                  </div>
                </div>
                <!-- Data sources -->
                <div class="min-w-0">
                  <div class="text-[11px] text-gray-500 mb-1">Data Sources</div>
                  <div class="flex flex-wrap gap-2">
                    <template v-for="dsId in (row.case.data_source_ids_json || [])" :key="dsId">
                      <div class="inline-flex items-center px-2 py-1 rounded border text-[11px]" v-if="dataSourceById[dsId]" :title="dataSourceById[dsId].name">
                        <DataSourceIcon :type="dataSourceById[dsId].type" class="w-3.5 h-3.5" />
                        <span class="ml-1 truncate max-w-[120px]">{{ dataSourceById[dsId].name }}</span>
                      </div>
                    </template>
                    <span v-if="!(row.case.data_source_ids_json || []).length" class="text-xs text-gray-500">—</span>
                  </div>
                </div>
                <!-- Files -->
                <div class="sm:col-span-2 min-w-0" v-if="(row.case.prompt_json?.files || []).length">
                  <div class="text-[11px] text-gray-500 mb-1">Files</div>
                  <div class="flex flex-wrap gap-2">
                    <div v-for="fid in (row.case.prompt_json?.files || [])" :key="fid" class="inline-flex items-center px-2 py-1 rounded border text-[11px]">
                      <Icon name="heroicons-document" class="w-3.5 h-3.5 text-gray-500" />
                      <span class="ml-1 truncate max-w-[200px]">{{ fileNameById[fid] || fid }}</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <!-- Right: Assertions -->
            <div class="p-4">
              <div class="text-xs text-gray-700 mb-2">Assertions</div>
              <div class="divide-y divide-gray-100">
                <div v-for="(rule, idx) in (row.case.expectations_json?.rules || [])" :key="idx" class="py-2">
                  <div class="flex items-center gap-2">
                    <span class="inline-flex items-center justify-center w-5 h-5 rounded-full"
                          :class="ruleIconClass(row.result.status)">
                      <Spinner v-if="row.result.status === 'in_progress'" class="w-3 h-3 text-gray-700" />
                      <svg v-else-if="row.result.status === 'error' || ruleFailed(row.result, idx)" xmlns="http://www.w3.org/2000/svg" class="w-3 h-3 text-red-700" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-10.293a1 1 0 00-1.414-1.414L10 8.586 7.707 6.293a1 1 0 00-1.414 1.414L8.586 10l-2.293 2.293a1 1 0 101.414 1.414L10 11.414l2.293 2.293a1 1 0 001.414-1.414L11.414 10l2.293-2.293z" clip-rule="evenodd"/></svg>
                      <svg v-else xmlns="http://www.w3.org/2000/svg" class="w-3 h-3 text-green-700" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 00-1.414-1.414L7 12.172 4.707 9.879a1 1 0 10-1.414 1.414l3 3a1 1 0 001.414 0l8-8z" clip-rule="evenodd"/></svg>
                    </span>
                    <div class="text-xs font-medium text-gray-800 truncate flex-1">
                      {{ summarizeRule(rule) }}
                    </div>
                    <div class="text-[11px] text-gray-500">{{ mockRuleDuration(row) }}</div>
                    <button class="text-blue-600 text-[11px] ml-2 hover:underline" @click="toggleExpanded(row.result.id, idx)">
                      {{ isExpanded(row.result.id, idx) ? 'Hide' : 'Show' }}
                    </button>
                  </div>
                  <div v-if="isExpanded(row.result.id, idx)" class="mt-2 bg-gray-50 rounded p-2 text-[11px] text-gray-700 overflow-x-auto">
                    <pre class="whitespace-pre-wrap break-words">{{ toPrettyJSON(rule) }}</pre>
                  </div>
                </div>
                <div v-if="(row.case.expectations_json?.rules || []).length === 0" class="py-2 text-xs text-gray-500">
                  No rules configured for this case.
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({
  layout: 'monitoring'
})

import LLMProviderIcon from '~/components/LLMProviderIcon.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import Spinner from '~/components/Spinner.vue'

const route = useRoute()
const runId = computed(() => String(route.params.id || ''))

type TestRun = {
  id: string
  suite_id: string
  trigger_reason?: string
  status: 'in_progress' | 'success' | 'error'
  started_at?: string
  finished_at?: string
  title?: string
}

type RuleEvidence = { type: 'create_data' | 'clarify' | 'completion' | 'judge', occurrence?: number, step_id?: string }

type RuleResult = { ok: boolean, message?: string, actual?: any, evidence?: RuleEvidence }

type ResultTotals = { total: number, passed: number, failed: number, duration_ms?: number | null }

type ResultJson = { totals: ResultTotals, rule_results: RuleResult[] }

type TestResult = { id: string, run_id: string, case_id: string, status: 'in_progress' | 'pass' | 'fail' | 'error', result_json?: ResultJson }

type TestCase = {
  id: string
  name: string
  prompt_json: any
  expectations_json: { spec_version: number, rules: any[] }
  data_source_ids_json?: string[]
}

const run = ref<TestRun | null>(null)
const results = ref<TestResult[]>([])
const suiteName = ref<string>('')
const caseRows = ref<{ result: TestResult, case: TestCase }[]>([])
const expanded = ref<Record<string, boolean>>({})
const openRows = ref<Record<string, boolean>>({})
const models = ref<any[]>([])
const modelById = computed<Record<string, any>>(() => Object.fromEntries((models.value || []).map((m: any) => [m.model_id || m.id, m])))
const dataSources = ref<any[]>([])
const dataSourceById = reactive<Record<string, any>>({})
const fileList = ref<any[]>([])
const fileNameById = reactive<Record<string, string>>({})

type RawLog = { ts: string, event: string, data: any, label: string, text: string, group?: string }
const logsByResultId = reactive<Record<string, RawLog[]>>({})

function ensureLogBuffer(resultId: string) {
  if (!logsByResultId[resultId]) logsByResultId[resultId] = []
}

function summarizeEvent(event: string, data: any): { label: string, text: string } {
  const upper = (s: string) => String(s || '').toUpperCase()
  const safeStr = (v: any) => {
    if (v == null) return ''
    if (typeof v === 'string') return v
    try { return JSON.stringify(v) } catch { return String(v) }
  }
  switch (event) {
    case 'run.started':
      return { label: 'RUN', text: 'Started' }
    case 'run.finished':
      return { label: 'RUN', text: `Finished${data?.status ? ` · status=${data.status}` : ''}` }
    case 'completion.started':
      return { label: 'COMPLETION', text: 'Started' }
    case 'completion.finished':
      return { label: 'COMPLETION', text: `Finished${data?.status ? ` · status=${data.status}` : ''}` }
    case 'completion.error':
      return { label: 'COMPLETION', text: `Error${data?.error ? ` · ${safeStr(data.error)}` : ''}` }
    case 'result.update':
      return { label: 'RESULT', text: `Update${data?.status ? ` · status=${data.status}` : ''}` }
    case 'block.upsert': {
      const title = data?.block?.title || data?.block?.id || 'block'
      const status = data?.block?.status
      return { label: 'BLOCK', text: `${title}${status ? ` · ${status}` : ''}` }
    }
    case 'decision.partial': {
      const r = (data?.reasoning || data?.plan_decision?.reasoning || data?.plan_reasoning || '')
      const assistant = data?.assistant
      const msg = r || assistant || ''
      return { label: 'DECISION.PARTIAL', text: msg ? String(msg) : '—' }
    }
    case 'decision.final': {
      const finalA = data?.final_answer || data?.assistant || ''
      return { label: 'DECISION.FINAL', text: finalA ? String(finalA) : '—' }
    }
    case 'tool.started':
      return { label: 'TOOL', text: `${data?.tool_name || 'tool'} started` }
    case 'tool.progress': {
      const stage = data?.payload?.stage
      return { label: 'TOOL', text: `${data?.tool_name || 'tool'}${stage ? ` · ${stage}` : ''}` }
    }
    case 'tool.partial': {
      const answer = (data?.payload?.answer || data?.payload?.delta || '').toString()
      return { label: 'TOOL', text: `${data?.tool_name || 'tool'} · ${answer}` }
    }
    case 'tool.finished': {
      const summary = data?.result_summary
      const status = data?.status
      return { label: 'TOOL', text: `${data?.tool_name || 'tool'} finished${status ? ` · ${status}` : ''}${summary ? ` · ${summary}` : ''}` }
    }
    default:
      return { label: upper(event), text: safeStr(data) }
  }
}

function groupFor(event: string, data: any): string | undefined {
  // Return a stable group key for events that should update in-place
  switch (event) {
    case 'run.started':
    case 'run.finished':
      return 'RUN'
    case 'completion.started':
    case 'completion.finished':
    case 'completion.error':
      return 'COMPLETION'
    case 'result.update':
      return 'RESULT'
    case 'decision.partial':
    case 'decision.final':
      return 'DECISION'
    case 'tool.started':
    case 'tool.progress':
    case 'tool.partial':
    case 'tool.finished': {
      const name = (data?.tool_name || '').toString() || 'TOOL'
      return `TOOL:${name}`
    }
    case 'block.upsert': {
      const bid = (data?.block?.id || '').toString()
      return bid ? `BLOCK:${bid}` : 'BLOCK'
    }
    default:
      return undefined
  }
}

function pushLog(resultId: string, event: string, data: any) {
  // Drop extremely noisy token deltas for the mini view
  if (event === 'block.delta.token') return
  try {
    ensureLogBuffer(resultId)
    const arr = logsByResultId[resultId]
    const summary = summarizeEvent(event, data)
    const group = groupFor(event, data)
    if (group) {
      // Replace the last entry with the same group (edit-in-place)
      let idx = -1
      for (let i = arr.length - 1; i >= 0; i--) {
        if (arr[i].group === group) { idx = i; break }
      }
      const item: RawLog = { ts: new Date().toISOString(), event, data, label: summary.label, text: summary.text, group }
      if (idx >= 0) {
        arr.splice(idx, 1, item)
      } else {
        arr.push(item)
      }
    } else {
      arr.push({ ts: new Date().toISOString(), event, data, label: summary.label, text: summary.text })
    }
    // Keep a bounded buffer per result
    if (arr.length > 200) arr.splice(0, arr.length - 200)
  } catch {}
}

function getLogs(resultId: string): RawLog[] {
  return logsByResultId[resultId] || []
}

const isExpanded = (resultId: string, idx: number) => {
  return !!expanded.value[`${resultId}:${idx}`]
}
const toggleExpanded = (resultId: string, idx: number) => {
  const key = `${resultId}:${idx}`
  expanded.value[key] = !expanded.value[key]
}

const isRowExpanded = (resultId: string) => {
  return !!openRows.value[resultId]
}
const toggleRow = (resultId: string) => {
  openRows.value[resultId] = !openRows.value[resultId]
}

const load = async () => {
  try {
    const [runRes, resRes, modelsRes, dsRes, filesRes] = await Promise.all([
      useMyFetch<TestRun>(`/api/tests/runs/${runId.value}`),
      useMyFetch<TestResult[]>(`/api/tests/runs/${runId.value}/results`),
      useMyFetch<any[]>(`/api/llm/models?is_enabled=true`),
      useMyFetch<any[]>(`/data_sources/active`),
      useMyFetch<any[]>(`/api/files`)
    ])
    run.value = runRes.data.value as any
    results.value = (resRes.data.value as any[]) || []
    // Initialize log buffers for all results
    for (const r of results.value) ensureLogBuffer(String(r.id))
    models.value = (modelsRes.data.value as any[]) || []
    dataSources.value = (dsRes.data.value as any[]) || []
    for (const ds of dataSources.value) dataSourceById[String(ds.id)] = ds
    const files = (filesRes.data.value as any[]) || []
    fileList.value = files
    for (const f of files) fileNameById[String(f.id)] = f.filename || f.name || String(f.id)

    // Fetch suite name and cases
    if (run.value?.suite_id) {
      const suiteRes: any = await useMyFetch(`/api/tests/suites/${run.value.suite_id}`)
      suiteName.value = suiteRes?.data?.value?.name || ''
    }
    // Fetch cases for each result
    const caseFetches = results.value.map(r => useMyFetch<TestCase>(`/api/tests/cases/${r.case_id}`))
    const caseResponses = await Promise.all(caseFetches)
    const casesById: Record<string, TestCase> = {}
    for (const cr of caseResponses) {
      const c = cr.data.value as any
      if (c?.id) casesById[c.id] = c
    }
    caseRows.value = results.value.map(r => ({ result: r, case: casesById[r.case_id] }))
  } catch (e) {
    console.error('Failed to load run', e)
  }
}

const runStatusClass = (status?: string) => {
  if (status === 'success' || status === 'pass') return 'bg-green-100 text-green-800'
  if (status === 'error' || status === 'fail') return 'bg-red-100 text-red-800'
  return 'bg-gray-100 text-gray-800'
}

const ruleIconClass = (status?: string) => {
  if (status === 'error' || status === 'fail') return 'bg-red-100'
  if (status === 'in_progress') return 'bg-gray-100'
  return 'bg-green-100'
}

const prettyStatus = (status?: string) => {
  if (!status) return '—'
  if (status === 'in_progress') return 'In progress'
  return status.replace('_', ' ')
}

const passCount = computed(() => results.value.filter(r => r.status === 'pass').length)
const failCount = computed(() => results.value.filter(r => r.status === 'fail').length)
const errorCount = computed(() => results.value.filter(r => r.status === 'error').length)

const formatDate = (iso?: string | null) => {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return '—'
  }
}

const formatDuration = (start?: string | null, end?: string | null) => {
  if (!start) return '—'
  const s = new Date(start).getTime()
  const e = end ? new Date(end).getTime() : Date.now()
  const ms = Math.max(0, e - s)
  const secs = Math.round(ms / 1000)
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  const rem = secs % 60
  return `${mins}m ${rem}s`
}

const assertionCount = (row: { result: TestResult, case: TestCase }) => {
  return (row.case.expectations_json?.rules || []).length
}

const modelProviderType = (modelId?: string) => {
  const m = modelById.value[modelId || '']
  return m?.provider?.provider_type || 'default'
}
const modelIdText = (modelId?: string) => {
  const m = modelById.value[modelId || '']
  return m?.model_id || modelId || 'default'
}
const modelProviderName = (modelId?: string) => {
  const m = modelById.value[modelId || '']
  return m?.provider?.name || m?.provider_name || ''
}

const summarizeRule = (rule: any) => {
  // Very small summary; can be improved
  try {
    const target = rule?.target?.field || rule?.target || 'rule'
    const type = rule?.matcher?.type || 'matcher'
    return `${target} · ${type}`
  } catch {
    return 'rule'
  }
}

const mockRuleDuration = (row: { result: TestResult, case: TestCase }) => {
  // Placeholder per-rule duration for UI; replace with real metrics later
  const base = 2 + (row.case.id.charCodeAt(0) % 5)
  return `${base}s`
}

const caseDuration = (row: { result: TestResult, case: TestCase }) => {
  // Prefer duration from result_json; otherwise a lightweight placeholder
  const ms = row.result.result_json && row.result.result_json.totals && typeof row.result.result_json.totals.duration_ms === 'number'
    ? Number(row.result.result_json.totals.duration_ms)
    : null
  if (typeof ms === 'number') {
    if (ms < 1000) return `${ms}ms`
    const secs = Math.round(ms / 1000)
    if (secs < 60) return `${secs}s`
    const mins = Math.floor(secs / 60)
    const rem = secs % 60
    return `${mins}m ${rem}s`
  }
  // Fallback mock based on rule count to avoid blank UI
  const rules = assertionCount(row)
  if (rules <= 0) return '—'
  const secs = Math.min(300, 2 * rules)
  return secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`
}

const toPrettyJSON = (v: any) => {
  try { return JSON.stringify(v, null, 2) } catch { return String(v) }
}

type ConversationMessage = { role: string, content: string }
const mockLogs = (row: { result: TestResult, case: TestCase }): ConversationMessage[] => {
  const caseName = row.case.name || 'Test Case'
  const prompt = typeof row.case.prompt_json?.content === 'string'
    ? row.case.prompt_json.content
    : ''
  const promptSnippet = prompt ? (prompt.length > 160 ? prompt.slice(0, 160) + '…' : prompt) : 'No prompt content provided.'
  return [
    { role: 'user', content: `Run "${caseName}" using the latest dataset.` },
    { role: 'assistant', content: 'Acknowledged. Gathering inputs and evaluating expectations…' },
    { role: 'assistant', content: `Initial prompt: ${promptSnippet}` }
  ]
}

const stopRun = async () => {
  try {
    if (!run.value?.id || run.value.status !== 'in_progress') return
    await useMyFetch(`/api/tests/runs/${run.value.id}/stop`, { method: 'POST' })
    await load()
  } catch (e) {
    console.error('Failed to stop run', e)
  }
}

onMounted(load)

// Start run-level streaming once the page is loaded
onMounted(async () => {
  try {
    // Small delay to ensure load() has populated results
    setTimeout(async () => {
      try {
        // Use fetch streaming (POST) - EventSource does not support POST
        const raw: any = await useMyFetch(`/tests/runs/${runId.value}/stream`, { method: 'POST', stream: true } as any)
        const res: Response = (raw?.data?.value ?? raw?.data) as unknown as Response
        const reader = res?.body?.getReader?.()
        if (!reader) return
        const decoder = new TextDecoder()
        let buffer = ''
        const processChunk = (text: string) => {
          buffer += text
          // Split SSE messages by double newline
          let idx
          while ((idx = buffer.indexOf('\n\n')) !== -1) {
            const raw = buffer.slice(0, idx)
            buffer = buffer.slice(idx + 2)
            // Parse minimal SSE format
            const lines = raw.split('\n')
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
                // Fan out a log entry to each result in the run
                const resList = Array.isArray((payload as any)?.results) ? (payload as any).results : []
                for (const it of resList) {
                  const rid = String((it as any)?.result_id || '')
                  if (rid) pushLog(rid, eventName, payload)
                }
              } else if (eventName === 'result.update') {
                const rid = String((payload as any).result_id || '')
                const idx = results.value.findIndex(r => String(r.id) === rid)
                if (idx >= 0) {
                  const copy = { ...results.value[idx] }
                  if ((payload as any).status) (copy as any).status = (payload as any).status
                  if ((payload as any).result_json) (copy as any).result_json = (payload as any).result_json
                  const tmp = [...results.value]
                  tmp[idx] = copy
                  results.value = tmp
                }
                if (rid) pushLog(rid, eventName, payload)
              } else if (eventName === 'completion.started' || eventName === 'completion.finished' || eventName === 'completion.error') {
                const rid = String((payload as any)?.result_id || '')
                if (rid) pushLog(rid, eventName, payload)
              } else if (eventName === 'run.finished') {
                if (run.value && (payload as any)?.status) (run.value as any).status = (payload as any).status
                // Broadcast finished to all known results
                for (const r of results.value) {
                  pushLog(String(r.id), eventName, payload)
                }
              } else {
                // Catch-all: record any other events (block.*, decision.*, tool.* etc.)
                const rid = String((payload as any)?.result_id || '')
                if (rid) {
                  pushLog(rid, eventName, payload)
                } else {
                  // If no result_id present, fan out to all results to avoid losing context
                  for (const r of results.value) {
                    pushLog(String(r.id), eventName, payload)
                  }
                }
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
      } catch (e) {
        console.error('Run stream failed', e)
      }
    }, 100)
  } catch {}
})

const ruleFailed = (result: TestResult, idx: number) => {
  const rr = result.result_json?.rule_results || []
  if (!Array.isArray(rr) || idx < 0 || idx >= rr.length) return false
  return rr[idx]?.ok === false
}
</script>



