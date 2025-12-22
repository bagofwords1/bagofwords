<template>
  <div class="flex justify-center pl-2 md:pl-4 text-sm">
    <div class="w-full max-w-7xl px-4 pl-0 py-2">
      <div class="mt-6">

        <!-- Run header -->
         <NuxtLink :to="'/evals'" class="text-blue-600 text-sm hover:underline ml-2 mt-2" >
          <Icon name="heroicons-arrow-left" class="w-4 h-4" />
          Back to Evals
        </NuxtLink>
        <div class="bg-white border border-gray-200 rounded-xl p-5 mb-6">
          <div class="flex flex-wrap items-start gap-3">
            <div class="min-w-0 flex-1">
              <div class="text-lg font-semibold text-gray-900 truncate">
                {{ run?.title || 'Test Run' }}
              </div>
              <div class="mt-1 text-xs text-gray-500 truncate">
                <span>Suite {{ suiteName || '—' }}</span>
                <span class="mx-1">|</span>
                <span>Triggered {{ timeAgo(run?.started_at) }} {{ prettyTriggerAdverb(run?.trigger_reason) }}</span>
                <span class="mx-1">|</span>
                <span>Total duration: {{ formatDuration(run?.started_at, run?.finished_at) }}</span>
                <template v-if="run?.build_id">
                  <span class="mx-1">|</span>
                  <span class="inline-flex items-center gap-1">
                    <Icon name="heroicons:cube" class="w-3 h-3" />
                    Build #{{ run?.build_number || '—' }}
                  </span>
                </template>
              </div>
            </div>
            <div class="ml-auto flex items-center gap-2">
              <span class="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-full" :class="runStatusClass(derivedRunStatus)">
                <Spinner v-if="derivedRunStatus === 'in_progress'" class="w-3 h-3" />
                {{ prettyStatus(derivedRunStatus) }}
              </span>
              <UButton v-if="run?.status === 'in_progress'" color="red" size="xs" variant="soft" icon="i-heroicons-stop" @click="stopRun">Stop</UButton>
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
              <div class="flex items-center gap-1 min-w-0">
                <!-- Pass/Fail icon -->
                <template v-if="row.result.status === 'in_progress'">
                  <Spinner class="w-4 h-4 text-gray-500" />
                </template>
                <template v-else-if="row.result.status === 'pass'">
                  <Icon name="heroicons-check" class="w-4 h-4 text-green-600" />
                </template>
                <template v-else-if="row.result.status === 'fail'">
                  <Icon name="heroicons-x-mark" class="w-4 h-4 text-red-600" />
                </template>
                <!-- X 4/6 Title -->
                <span class="text-xs font-regular text-gray-500 truncate">
                  {{ passedAssertions(row) }}/{{ assertionCount(row) }}
                </span>
                <span class="text-sm font-medium text-gray-900 truncate">
                  {{ row.case.name }}
                </span>
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
                  <div class="flex items-center justify-between">
                    <div class="text-[11px] text-gray-500">Prompt</div>
                  </div>
                  <pre class="whitespace-pre-wrap break-words bg-gray-50 rounded p-3 text-xs">{{ row.case.prompt_json?.content || '—' }}</pre>
                  <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <!-- Logs (now below Prompt) -->
                    <div class="sm:col-span-2">
                      <div class="flex items-center justify-between">
                        <div class="text-[11px] text-gray-500">
                          Logs
                          <NuxtLink
                            v-if="row.result.report_id"
                            :to="`/reports/${row.result.report_id}`"
                            target="_blank"
                            class="ml-2 text-blue-600 hover:underline text-[10px]"
                          >
                            Open report
                          </NuxtLink>
                        </div>
                        <span class="inline-flex items-center gap-1 px-2 py-1 text-[10px] font-medium rounded-full" :class="completionStatus(row.result.id).className">
                          <Spinner v-if="completionStatus(row.result.id).text === 'Running'" class="w-3 h-3" />
                          {{ completionStatus(row.result.id).text }}
                        </span>
                      </div>
                      <div
                        class="bg-gray-50 rounded p-3 text-xs max-h-80 overflow-y-auto"
                        :ref="(el) => setLogContainerRef(row.result.id, el)"
                        :id="`logs-${row.result.id}`"
                      >
                        <div class="space-y-1">
                          <div v-if="(getLogs(row.result.id) || []).length === 0" class="text-gray-500">—</div>
                          <div v-for="(e, mi) in getLogs(row.result.id)" :key="mi" class="text-gray-800 whitespace-pre-wrap break-words leading-relaxed">{{ e.text }}</div>
                        </div>
                      </div>
                    </div>
                    <!-- Model -->
                    <div class="min-w-0">
                      <div class="text-[11px] text-gray-500 mb-1">Model</div>
                      <div class="flex items-center gap-2">
                        <LLMProviderIcon :provider="modelProviderType(row.case.prompt_json?.model_id, row.case)" :icon="true" class="w-4 h-4" />
                        <div class="min-w-0">
                          <div class="text-xs text-gray-900 truncate">{{ modelDisplayName(row.case.prompt_json?.model_id, row.case) }}</div>
                          <div class="text-[10px] text-gray-500 truncate" v-if="modelProviderName(row.case.prompt_json?.model_id, row.case)">{{ modelProviderName(row.case.prompt_json?.model_id, row.case) }}</div>
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
                  <div class="text-xs text-gray-700 mb-2">Expectations</div>
                  <div class="space-y-2">
                    <div v-for="it in displayRules(row)" :key="it.originalIdx" class="border border-gray-200 rounded-md p-3">
                      <!-- Type -->
                      <div class="inline-flex items-center px-2 py-0.5 rounded-full border text-[11px] mb-1" :class="badgeClassesFor(categoryName(it.rule?.target?.category || ''))">
                        {{ categoryName(it.rule?.target?.category || '') }}
                      </div>
                      <!-- Assertion / Actual / Reasoning -->
                      <div class="text-xs text-gray-900">
                        <span class="text-[11px] text-gray-500">Assertion:</span>
                        {{ ruleSummaryText(it.rule) }}
                      </div>
                      <div class="text-xs text-gray-900 mt-0.5">
                        <span class="text-[11px] text-gray-500">Actual:</span>
                        {{ ruleActualText(row, it.originalIdx) || '—' }}
                      </div>
                      <div v-if="isJudgeRule(it.rule) && ruleReasoningText(row, it.originalIdx)" class="text-xs text-gray-900 mt-0.5">
                        <span class="text-[11px] text-gray-500">Reasoning:</span>
                        {{ ruleReasoningText(row, it.originalIdx) }}
                      </div>
                      <!-- Status line -->
                      <div class="flex items-center gap-2 mt-1">
                        <template v-if="ruleStatus(row, it.originalIdx) === 'pending'">
                          <Spinner class="w-3 h-3 text-gray-600" />
                          <span class="text-[11px] text-gray-600">Pending…</span>
                        </template>
                        <template v-else-if="ruleStatus(row, it.originalIdx) === 'skipped'">
                          <svg xmlns="http://www.w3.org/2000/svg" class="w-3 h-3 text-gray-500" viewBox="0 0 20 20" fill="currentColor"><path d="M10 3a7 7 0 100 14 7 7 0 000-14zM8 9h4v2H8V9z"/></svg>
                          <span class="text-[11px] text-gray-600">Skipped</span>
                        </template>
                        <template v-else-if="ruleStatus(row, it.originalIdx) === 'pass'">
                          <svg xmlns="http://www.w3.org/2000/svg" class="w-3 h-3 text-green-700" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 00-1.414-1.414L7 12.172 4.707 9.879a1 1 0 10-1.414 1.414l3 3a1 1 0 001.414 0l8-8z" clip-rule="evenodd"/></svg>
                          <span class="text-[11px] text-green-700">Pass</span>
                        </template>
                        <template v-else>
                          <svg xmlns="http://www.w3.org/2000/svg" class="w-3 h-3 text-red-700" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-10.293a1 1 0 00-1.414-1.414L10 8.586 7.707 6.293a1 1 0 00-1.414 1.414L8.586 10l-2.293 2.293a1 1 0 101.414 1.414L10 11.414l2.293 2.293a1 1 0 001.414-1.414L11.414 10l2.293-2.293z" clip-rule="evenodd"/></svg>
                          <span class="text-[11px] text-red-700">Fail</span>
                          <span v-if="ruleMessage(row, it.originalIdx)" class="text-[11px] text-red-700">· {{ ruleMessage(row, it.originalIdx) }}</span>
                        </template>
                      </div>
                    </div>
                    <div v-if="assertionCount(row) === 0" class="text-xs text-gray-500">No rules configured for this case.</div>
                  </div>
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
  auth: true,
  layout: 'default',
  permissions: ['manage_tests']
})

import LLMProviderIcon from '~/components/LLMProviderIcon.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import Spinner from '~/components/Spinner.vue'

const route = useRoute()
const runId = computed(() => String(route.params.id || ''))

type TestRun = {
  id: string
  suite_ids?: string
  trigger_reason?: string
  status: 'in_progress' | 'success' | 'error'
  started_at?: string
  finished_at?: string
  title?: string
  // Build system
  build_id?: string
  build_number?: number
}

type RuleEvidence = { type: 'create_data' | 'clarify' | 'completion' | 'judge', occurrence?: number, step_id?: string }

type RuleResult = { ok: boolean, status?: 'pass' | 'fail' | 'skipped', message?: string, actual?: any, evidence?: RuleEvidence }

type ResultTotals = { total: number, passed: number, failed: number, skipped?: number, duration_ms?: number | null }

type ResultSpec = { spec_version?: number, rules?: any[] }
type ResultJson = { totals: ResultTotals, rule_results: RuleResult[], spec?: ResultSpec }

type TestResult = { id: string, run_id: string, case_id: string, status: 'in_progress' | 'pass' | 'fail' | 'error', result_json?: ResultJson }
  & { report_id?: string }

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
type CaseRow = { result: TestResult, case: TestCase }
const caseRows = ref<CaseRow[]>([])
const expanded = ref<Record<string, boolean>>({})
const openRows = ref<Record<string, boolean>>({})
const models = ref<any[]>([])
const modelById = computed<Record<string, any>>(() => Object.fromEntries((models.value || []).map((m: any) => [m.model_id || m.id, m])))
const dataSources = ref<any[]>([])
const dataSourceById = reactive<Record<string, any>>({})
const fileList = ref<any[]>([])
const fileNameById = reactive<Record<string, string>>({})

// Derive the first suite id from run (supports both suite_id and suite_ids)
const suiteId = computed<string>(() => {
  try {
    const raw: any = (run.value as any)?.suite_id ?? (run.value as any)?.suite_ids ?? ''
    if (!raw) return ''
    return String(raw).split(',')[0].trim()
  } catch {
    return ''
  }
})

type RawLog = { ts: string, event: string, data: any, label: string, text: string, group?: string }
const logsByResultId = reactive<Record<string, RawLog[]>>({})
const toolInputCache = reactive<Record<string, string>>({})
const logContainerRefs = reactive<Record<string, HTMLElement | null>>({})

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
  const pickText = (d: any): string => {
    try {
      if (d == null) return ''
      if (typeof d === 'string') return d
      if (typeof d.text === 'string') return d.text
      if (typeof d.message === 'string') return d.message
      if (typeof d.content === 'string') return d.content
      if (d.payload) {
        if (typeof d.payload.text === 'string') return d.payload.text
        if (typeof d.payload.message === 'string') return d.payload.message
        if (typeof d.payload.content === 'string') return d.payload.content
      }
      if (d.block) {
        if (typeof d.block.text === 'string') return d.block.text
        if (typeof d.block.content === 'string') return d.block.content
      }
      return ''
    } catch { return '' }
  }
  switch (event) {
    case 'seed.reasoning': {
      // Lightweight non-SSE preload for reasoning
      const t = pickText(data)
      return { label: 'DECISION', text: t ? `Thinking: ${t}` : '' }
    }
    case 'seed.content': {
      // Lightweight non-SSE preload for assistant content
      const t = pickText(data)
      return { label: 'BLOCK', text: t ? `Completion: ${t}` : '' }
    }
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
      const text = pickText(data)
      if (text) return { label: 'BLOCK', text: `Completion: ${text}` }
      const title = data?.block?.title || data?.block?.id || 'block'
      const status = data?.block?.status
      return { label: 'BLOCK', text: `${title}${status ? ` · ${status}` : ''}` }
    }
    case 'block.partial':
    case 'block.update':
    case 'block.content':
    case 'block.reasoning':
      return { label: 'BLOCK', text: pickText(data) || '' }
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
    default: {
      const t = pickText(data)
      // Do not dump raw objects into the mini log; ignore if no concise text
      return { label: upper(event), text: t || '' }
    }
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
  // Suppress granular/duplicative events to keep logs light
  if (event === 'block.delta.text' || event === 'block.delta.text.complete' || event === 'block.partial' || event === 'block.update' || event === 'block.content' || event === 'block.reasoning' || event === 'decision.partial' || event === 'block.delta.artifact' || event === 'data_model.completed') return
  // Only show final tool outcome; suppress started/progress/partial
  if ((event === 'tool.started' || event === 'tool.progress' || event === 'tool.partial')) return
  try {
    ensureLogBuffer(resultId)
    const arr = logsByResultId[resultId]
    // Special compact formatting for tool calls: "name(input) -> output"
    if (event === 'tool.finished') {
      const name = (data?.tool_name || 'tool').toString()
      const stringify = (v: any) => {
        if (v == null) return ''
        if (typeof v === 'string') return v
        try { return JSON.stringify(v) } catch { return String(v) }
      }
      const inputVal = data?.payload?.input ?? data?.input ?? data?.args ?? ''
      const outputVal = data?.result_summary ?? data?.payload?.output ?? data?.output ?? data?.result ?? ''
      const cachedIn = toolInputCache[`${resultId}:TOOL:${name}`] || stringify(inputVal)
      const outText = stringify(outputVal)
      const text = event === 'tool.finished'
        ? `${name}(${cachedIn}) -> ${outText}`
        : `${name}(${cachedIn})`
      const item: RawLog = { ts: new Date().toISOString(), event, data, label: 'TOOL', text, group: `TOOL:${name}` }
      // Push duplicate entries instead of replacing, to keep a simple chronological log
      const last = arr[arr.length - 1]
      if (!last || !(last.event === item.event && last.text === item.text)) {
        arr.push(item)
      }
      if (arr.length > 200) arr.splice(0, arr.length - 200)
      setTimeout(() => scrollLogsToBottom(resultId), 0)
      return
    }
    const summary = summarizeEvent(event, data)
    if (!summary.text || !String(summary.text).trim()) return
    // Push event as its own entry; skip if identical to the immediately previous line
    const nextItem: RawLog = { ts: new Date().toISOString(), event, data, label: summary.label, text: summary.text, group: groupFor(event, data) }
    const prev = arr[arr.length - 1]
    if (!prev || !(prev.event === nextItem.event && prev.text === nextItem.text)) {
      arr.push(nextItem)
    }
    // Keep a bounded buffer per result
    if (arr.length > 200) arr.splice(0, arr.length - 200)
    setTimeout(() => scrollLogsToBottom(resultId), 0)
  } catch {}
}

function getLogs(resultId: string): RawLog[] {
  return logsByResultId[resultId] || []
}

function setLogContainerRef(resultId: string, el: any) {
  logContainerRefs[resultId] = (el as HTMLElement) || null
}

function scrollLogsToBottom(resultId: string) {
  const el = logContainerRefs[resultId] || (document.getElementById(`logs-${resultId}`) as HTMLElement | null)
  if (!el) return
  try {
    el.scrollTop = el.scrollHeight
  } catch {}
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
  if (openRows.value[resultId]) {
    nextTick(() => setTimeout(() => scrollLogsToBottom(resultId), 0))
  }
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
    const sid = suiteId.value
    if (sid) {
      const suiteRes: any = await useMyFetch(`/api/tests/suites/${sid}`)
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

    // Seed logs from recent completions (non-SSE fallback, scoped to latest system completion only)
    try {
      const statusRes: any = await useMyFetch(`/api/tests/runs/${runId.value}/status?limit=10`)
      const payload = (statusRes?.data?.value || {}) as any
      const items = Array.isArray(payload?.results) ? payload.results : []
      for (const it of items) {
        const rid = String(it?.result?.id || '')
        if (!rid) continue
        ensureLogBuffer(rid)
        const comps: any[] = Array.isArray(it?.completions) ? it.completions : []
        // Sort ascending by created_at if present
        comps.sort((a, b) => {
          const ta = new Date(a?.created_at || 0).getTime()
          const tb = new Date(b?.created_at || 0).getTime()
          return ta - tb
        })
        // Only consider the latest system completion for seeding logs to avoid stale "finished" states
        const latestSystem = [...comps].filter(c => (c?.role || '') === 'system').pop()
        if (latestSystem) {
          // Start
          pushLog(rid, 'completion.started', { result_id: rid, status: latestSystem?.status, system_completion_id: latestSystem?.id })
          // Reasoning/content (single snapshot, trimmed)
          const blocks = Array.isArray(latestSystem?.completion_blocks) ? latestSystem.completion_blocks : []
          for (const b of blocks) {
            const reasoning = (b?.plan_decision?.reasoning || b?.reasoning || '')
            const content = (b?.content || '')
            const trim = (txt: string) => {
              const s = String(txt || '')
              return s.length > 220 ? s.slice(0, 220) + '…' : s
            }
            if (reasoning) pushLog(rid, 'seed.reasoning', { text: trim(reasoning) })
            if (content) pushLog(rid, 'seed.content', { text: trim(content) })
            const te = b?.tool_execution
            if (te && te.tool_name) {
              pushLog(rid, 'tool.finished', {
                result_id: rid,
                tool_name: te.tool_name,
                status: te.status,
                result_summary: te.result_summary
              })
            }
          }
          // Only mark finished if the latest system completion is in a terminal state
          const terminal = new Set(['success', 'error', 'stopped', 'fail', 'pass'])
          if (terminal.has(String(latestSystem?.status || ''))) {
            pushLog(rid, 'completion.finished', { result_id: rid, status: latestSystem?.status })
          }
        }
        // Auto-scroll once per result
        setTimeout(() => scrollLogsToBottom(rid), 0)
      }
    } catch {}
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
  if (status === 'success') return 'Success'
  if (status === 'fail') return 'Failed'
  if (status === 'error') return 'Error'
  return status.replace('_', ' ')
}

const passCount = computed(() => results.value.filter(r => r.status === 'pass').length)
const failCount = computed(() => results.value.filter(r => r.status === 'fail').length)
const errorCount = computed(() => results.value.filter(r => r.status === 'error').length)

// Derive run status from individual result statuses to avoid mismatches with backend aggregate
const derivedRunStatus = computed<'in_progress' | 'success' | 'fail' | 'error'>(() => {
  try {
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

const formatDate = (iso?: string | null) => {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return '—'
  }
}

const timeAgo = (iso?: string | null) => {
  if (!iso) return '—'
  try {
    const then = new Date(iso).getTime()
    const now = Date.now()
    const diffSec = Math.max(0, Math.floor((now - then) / 1000))
    if (diffSec < 60) return `${diffSec}s ago`
    const mins = Math.floor(diffSec / 60)
    if (mins < 60) return `${mins}m ago`
    const hours = Math.floor(mins / 60)
    if (hours < 24) return `${hours}h ago`
    const days = Math.floor(hours / 24)
    return `${days}d ago`
  } catch {
    return '—'
  }
}

const prettyTriggerAdverb = (reason?: string | null) => {
  const r = String(reason || 'manual').toLowerCase()
  if (r === 'manual') return 'manually'
  return r
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

const specRulesForRow = (row: CaseRow): any[] => {
  const specRules = row.result.result_json?.spec?.rules
  if (Array.isArray(specRules)) return specRules
  return row.case.expectations_json?.rules || []
}

const displayRules = (row: CaseRow): Array<{ rule: any, originalIdx: number }> => {
  const rules = specRulesForRow(row)
  const out: Array<{ rule: any, originalIdx: number }> = []
  for (let i = 0; i < rules.length; i++) {
    const r = rules[i]
    // Hide Judge model_id - it's a configuration, not a pass/fail assertion
    if (r?.target?.category === 'judge' && r?.target?.field === 'model_id') continue
    out.push({ rule: r, originalIdx: i })
  }
  return out
}

const assertionCount = (row: CaseRow) => {
  return displayRules(row).length
}

const modelProviderType = (modelId?: string, caseObj?: TestCase) => {
  const m = modelById.value[String(modelId || '')]
  if (m) return m?.provider?.provider_type || 'default'
  const ms: any = (caseObj as any)?.model_summary
  return ms?.provider_type || 'default'
}
const modelDisplayName = (modelId?: string, caseObj?: TestCase) => {
  const m = modelById.value[String(modelId || '')]
  if (m) return m?.name || m?.model_id || modelId || 'default'
  const ms: any = (caseObj as any)?.model_summary
  return ms?.name || ms?.model_id || modelId || 'default'
}
const modelProviderName = (modelId?: string, caseObj?: TestCase) => {
  const m = modelById.value[String(modelId || '')]
  if (m) return m?.provider?.name || m?.provider_name || ''
  const ms: any = (caseObj as any)?.model_summary
  return ms?.provider_name || ''
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

const mockRuleDuration = (row: CaseRow) => {
  // Placeholder per-rule duration for UI; replace with real metrics later
  const base = 2 + (row.case.id.charCodeAt(0) % 5)
  return `${base}s`
}

const caseDuration = (row: CaseRow) => {
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

// ---- Read-only expectations helpers ----
const CATEGORY_LABELS: Record<string, string> = {
  'tool:create_data': 'Create Data',
  'tool:clarify': 'Clarify',
  'tool:describe_table': 'Describe Table',
  'metadata': 'Metadata',
  'completion': 'Completion',
  'judge': 'Judge',
}
const categoryName = (cat?: string) => {
  const c = String(cat || '')
  if (!c) return ''
  if (CATEGORY_LABELS[c]) return CATEGORY_LABELS[c]
  if (c.startsWith('tool:')) {
    const raw = c.split(':')[1] || ''
    const spaced = raw.replace(/_/g, ' ')
    return spaced.replace(/\b\w/g, (m) => m.toUpperCase())
  }
  return c
}
const humanize = (s?: string) => {
  const t = String(s || '')
  return t.replace(/_/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase())
}
const opLabel = (op?: string) => {
  switch (op) {
    case 'text.contains': return 'text contains'
    case 'text.not_contains': return 'text not contains'
    case 'text.equals': return 'text equals'
    case 'text.regex': return 'text matches regex'
    case 'number.cmp': return 'number compare'
    case 'length.cmp': return 'length compare'
    case 'list.contains': return 'list contains value'
    case 'list.contains_any': return 'list contains any'
    case 'list.contains_all': return 'list contains all'
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
const badgeClassesFor = (catLabel: string): string => {
  const map: Record<string, string> = {
    'Create Data': 'bg-blue-50 text-blue-700 border-blue-100',
    'Clarify': 'bg-amber-50 text-amber-700 border-amber-100',
    'Describe Table': 'bg-teal-50 text-teal-700 border-teal-100',
    'Metadata': 'bg-slate-50 text-slate-700 border-slate-100',
    'Completion': 'bg-purple-50 text-purple-700 border-purple-100',
    'Judge': 'bg-gray-100 text-gray-700 border-gray-200',
  }
  return map[catLabel] || 'bg-zinc-50 text-zinc-700 border-zinc-100'
}

// ---- Expectation summary and status helpers ----
const quote = (s: string) => `"${s}"`
const joinedQuoted = (arr: any[]) => quote(arr.map((v) => String(v)).join('; '))
const ruleSummaryText = (rule: any): string => {
  try {
    // For judge expectations, display the actual prompt text (expected value)
    if (isJudgeRule(rule)) {
      const val = rule?.matcher?.value ?? rule?.target?.value
      return typeof val === 'string' ? val : String(val ?? 'Prompt')
    }
    const field = humanize(rule?.target?.field || '')
    const op = opLabel(rule?.matcher?.type)
    const m = rule?.matcher || {}
    if (m?.type === 'list.contains_any' || m?.type === 'list.contains_all') {
      const vals = Array.isArray(m?.values) ? m.values : []
      return `${field} ${op} ${joinedQuoted(vals)}`
    }
    if (m?.type === 'text.regex') {
      const pat = String(m?.pattern || '')
      return `${field} ${op} /${pat}/`
    }
    if (m?.type === 'number.cmp' || m?.type === 'length.cmp') {
      return `${field} ${op} ${cmpSymbol(m?.op)} ${m?.value}`
    }
    // text.* and list.contains use value
    const val = String(m?.value ?? '')
    return `${field} ${op} ${quote(val)}`
  } catch {
    return '—'
  }
}
const ruleResultAt = (row: { result: TestResult }, idx: number) => {
  const rr = row.result.result_json?.rule_results || []
  if (!Array.isArray(rr) || idx < 0 || idx >= rr.length) return null
  return rr[idx] || null
}
const ruleStatus = (row: { result: TestResult }, idx: number): 'pending' | 'pass' | 'fail' | 'skipped' => {
  if (row.result.status === 'in_progress') return 'pending'
  const rr = ruleResultAt(row, idx)
  if (!rr || typeof rr.ok !== 'boolean') return 'pending'
  if (rr.status === 'skipped') return 'skipped'
  return rr.ok ? 'pass' : 'fail'
}
const ruleMessage = (row: { result: TestResult }, idx: number): string => {
  const rr = ruleResultAt(row, idx)
  return (rr && typeof rr.message === 'string') ? rr.message : ''
}

// Completion status (from streaming events) for the logs badge
const completionStatus = (resultId: string): { text: string, className: string } => {
  try {
    const logs = getLogs(resultId)
    let text = '—'
    if (logs.some(l => l.event === 'completion.error')) text = 'Error'
    else if (logs.some(l => l.event === 'completion.finished')) text = 'Finished'
    else if (logs.some(l => l.event === 'completion.started')) text = 'Running'
    const classMap: Record<string, string> = {
      'Running': 'bg-blue-100 text-blue-800',
      'Finished': 'bg-green-100 text-green-800',
      'Error': 'bg-red-100 text-red-800',
      '—': 'bg-gray-100 text-gray-800'
    }
    return { text, className: classMap[text] || classMap['—'] }
  } catch {
    return { text: '—', className: 'bg-gray-100 text-gray-800' }
  }
}

// ---- Additional details for UI: Actual and Judge Reasoning ----
const ruleActualText = (row: { result: TestResult }, idx: number): string => {
  const rr: any = ruleResultAt(row, idx) as any
  const actual = rr?.actual
  if (actual == null) return ''
  if (typeof actual === 'string') return actual
  if (Array.isArray(actual)) return actual.map((v) => String(v)).join('; ')
  try { return JSON.stringify(actual) } catch { return String(actual) }
}
const isJudgeRule = (rule: any): boolean => {
  return String(rule?.target?.category || '') === 'judge'
}
const ruleReasoningText = (row: { result: TestResult }, idx: number): string => {
  const rr: any = ruleResultAt(row, idx) as any
  return (rr?.evidence?.reasoning || rr?.message || '') as string
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

// ------- Results refresh helpers (debounced) -------
const refreshTimer = ref<any>(null)
const isTerminalStatus = (s?: string | null) => {
  const v = String(s || '')
  return v === 'pass' || v === 'fail' || v === 'error' || v === 'success'
}
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
    // Rebuild case rows with possibly updated cases count/order
    const caseFetches = newResults.map(r => useMyFetch<TestCase>(`/api/tests/cases/${r.case_id}`))
    const caseResponses = await Promise.all(caseFetches)
    const casesById: Record<string, TestCase> = {}
    for (const cr of caseResponses) {
      const c = cr.data.value as any
      if (c?.id) casesById[c.id] = c
    }
    caseRows.value = newResults.map(r => ({ result: r, case: casesById[r.case_id] }))
  } catch (e) {
    console.error('Failed to reload results', e)
  }
}
const scheduleResultsRefresh = (withRun: boolean = false, delayMs: number = 400) => {
  try {
    if (refreshTimer.value) {
      clearTimeout(refreshTimer.value)
    }
    refreshTimer.value = setTimeout(async () => {
      try {
        if (withRun) await reloadRun()
        await reloadResults()
      } finally {
        refreshTimer.value = null
      }
    }, Math.max(0, delayMs || 0))
  } catch {}
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
                // If this is a terminal update, schedule a full results refresh
                const st = (payload as any)?.status
                if (isTerminalStatus(st)) scheduleResultsRefresh(false)
              } else if (eventName === 'completion.started' || eventName === 'completion.finished' || eventName === 'completion.error') {
                const rid = String((payload as any)?.result_id || '')
        if (rid) pushLog(rid, eventName, payload)
        // Ensure UI refreshes even if a terminal completion doesn't emit a result.update
        if (eventName === 'completion.finished' || eventName === 'completion.error') {
          scheduleResultsRefresh(false, 600)
        }
              } else if (eventName === 'block.upsert') {
                // Special-case: show both reasoning and content when present
                const rid = String((payload as any)?.result_id || '')
                const block = (payload as any)?.block || {}
                const reasoning = (block?.plan_decision?.reasoning || block?.reasoning || '')
                const content = (block?.content || '')
                if (rid) {
                  if (reasoning) pushLog(rid, 'seed.reasoning', { text: reasoning })
                  if (content) pushLog(rid, 'seed.content', { text: content })
                  // Also record the upsert itself for title/status changes
                  pushLog(rid, eventName, payload)
                }
              } else if (eventName === 'run.finished') {
                if (run.value && (payload as any)?.status) (run.value as any).status = (payload as any).status
                // Broadcast finished to all known results
                for (const r of results.value) {
                  pushLog(String(r.id), eventName, payload)
                }
                // Re-fetch run header and results once the run is finished
                scheduleResultsRefresh(true)
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
        // Stream ended; ensure we refresh the latest run/results state
        scheduleResultsRefresh(true)
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

// Passed assertions counter per row (only counting visible rules)
const passedAssertions = (row: CaseRow): number => {
  try {
    const rr = row.result.result_json?.rule_results || []
    if (!Array.isArray(rr)) return 0
    const visible = displayRules(row).map(it => it.originalIdx)
    let cnt = 0
    for (const idx of visible) {
      const entry = rr[idx]
      if (entry && entry.ok === true) cnt++
    }
    return cnt
  } catch {
    return 0
  }
}
</script>




