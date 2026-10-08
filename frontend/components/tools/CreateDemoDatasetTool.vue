<template>
  <div class="mt-1" data-testid="create-demo-dataset-tool">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="phase === 'designing'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-sparkles" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ $t('tools.createDemoDataset.designing', { name: args.name || '' }) }}</span>
        </span>
        <span v-else-if="phase === 'review'" class="flex items-center text-gray-700 dark:text-gray-300">
          <Icon name="heroicons-clipboard-document-check" class="w-3 h-3 me-1 text-blue-500" />
          <span>{{ $t('tools.createDemoDataset.review') }}</span>
        </span>
        <span v-else-if="phase === 'working'" class="flex items-center">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
          <span class="tool-shimmer">{{ workingLabel }}</span>
        </span>
        <span v-else-if="phase === 'created'" class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-sparkles" class="w-3 h-3 me-1 text-emerald-500" />
          <span>{{ $t('tools.createDemoDataset.created') }}</span>
        </span>
        <span v-else-if="phase === 'rejected'" class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-arrow-uturn-left" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ outputStatus === 'timed_out' ? $t('tools.createDemoDataset.timedOut') : $t('tools.createDemoDataset.rejected') }}</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-amber-500" />
          <span>{{ $t('tools.createDemoDataset.failed') }}</span>
        </span>
      </div>
    </Transition>

    <div v-if="phase === 'failed' && message" class="text-xs text-gray-500 dark:text-gray-400 ms-1 mb-2 max-w-2xl">
      {{ message }}
      <ul v-if="errors.length" class="mt-1 list-disc ms-4 space-y-0.5">
        <li v-for="e in errors" :key="e">{{ e }}</li>
      </ul>
    </div>
    <div v-if="phase === 'rejected' && feedback" class="text-xs text-gray-500 dark:text-gray-400 ms-1 mb-2 italic">
      “{{ feedback }}”
    </div>

    <!-- Dataset card -->
    <Transition name="fade" appear>
      <div
        v-if="tables.length"
        class="rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 max-w-2xl"
        data-testid="demo-dataset-card"
      >
        <!-- Header -->
        <div class="px-3 pt-2.5 pb-2">
          <div class="flex items-center gap-2">
            <span v-if="datasetEmoji" class="text-base leading-none flex-shrink-0">{{ datasetEmoji }}</span>
            <Icon v-else name="heroicons-circle-stack" class="w-4 h-4 text-blue-500 flex-shrink-0" />
            <span class="text-sm font-medium text-gray-800 dark:text-gray-200 truncate" dir="auto">{{ connectionName || args.name }}</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 flex-shrink-0">
              {{ $t('tools.createDemoDataset.badge') }}
            </span>
            <span class="flex-1"></span>
            <span v-if="dateRange" class="hidden sm:inline text-[10px] text-gray-400 flex-shrink-0">{{ dateRange }}</span>
          </div>
          <div v-if="args.description || args.domain" class="mt-1 text-[11px] text-gray-500 dark:text-gray-400 leading-snug" dir="auto">
            {{ args.description || args.domain }}
          </div>
        </div>

        <!-- Tables -->
        <div v-if="expanded" class="border-t border-gray-100 dark:border-gray-800 px-3 py-2">
          <div class="flex items-center text-[10px] uppercase tracking-wide text-gray-400 mb-1">
            <span>{{ $t('tools.createDemoDataset.tables', { count: tables.length }) }}</span>
            <span class="flex-1"></span>
            <span v-if="phase === 'working' && progressTotal" class="normal-case tracking-normal">
              {{ $t('tools.createDemoDataset.progress', { done: progressDone, total: progressTotal }) }}
            </span>
            <span v-else class="normal-case tracking-normal">{{ formatRows(rowsTotal) }} {{ $t('tools.createDemoDataset.rows') }}</span>
          </div>
          <div v-if="phase === 'working' && progressTotal" class="h-0.5 rounded bg-gray-100 dark:bg-gray-800 overflow-hidden mb-1.5">
            <div class="h-full bg-blue-500 transition-all duration-500" :style="{ width: `${Math.round(100 * progressDone / progressTotal)}%` }"></div>
          </div>
          <ul class="space-y-0.5">
            <li v-for="t in tables" :key="t.name" class="text-[11px]">
              <button
                class="w-full flex items-center gap-2 py-0.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800/60 text-start"
                @click="toggleTable(t.name)"
              >
                <span class="w-3.5 h-3.5 flex items-center justify-center flex-shrink-0">
                  <Spinner v-if="['writing_code', 'running', 'retrying'].includes(tableState(t.name).state)" class="w-3 h-3 text-blue-500" />
                  <Icon v-else-if="tableState(t.name).state === 'done'" name="heroicons-check" class="w-3 h-3 text-emerald-500" />
                  <Icon v-else name="heroicons-table-cells" class="w-3 h-3 text-gray-400" />
                </span>
                <span class="font-mono text-gray-700 dark:text-gray-300 truncate">{{ t.name }}</span>
                <span class="text-gray-400 truncate hidden sm:inline" dir="auto">{{ t.description }}</span>
                <span class="flex-1"></span>
                <span v-if="tableState(t.name).state === 'retrying'" class="text-[10px] text-amber-600 dark:text-amber-400 flex-shrink-0">
                  {{ $t('tools.createDemoDataset.retry', { n: tableState(t.name).attempt }) }}
                </span>
                <span
                  v-if="['writing_code', 'running'].includes(tableState(t.name).state)"
                  class="h-2 w-12 rounded bg-gray-100 dark:bg-gray-800 animate-pulse flex-shrink-0"
                ></span>
                <span v-else class="text-[10px] text-gray-400 tabular-nums flex-shrink-0">
                  {{ formatRows(tableRows(t)) }}
                </span>
                <Icon :name="openTable === t.name ? 'heroicons-chevron-up' : 'heroicons-chevron-down'" class="w-2.5 h-2.5 text-gray-300 flex-shrink-0" />
              </button>
              <div v-if="openTable === t.name" class="ms-5 mb-1 mt-0.5 flex flex-wrap gap-1">
                <span
                  v-for="c in t.columns" :key="c.name"
                  class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-gray-50 dark:bg-gray-800 text-gray-600 dark:text-gray-400"
                  :title="c.description || ''"
                >
                  <Icon v-if="c.primary_key" name="heroicons-key" class="w-2.5 h-2.5 text-amber-500" />
                  <Icon v-else-if="c.references" name="heroicons-link" class="w-2.5 h-2.5 text-blue-400" />
                  <span class="font-mono">{{ c.name }}</span>
                  <span class="text-gray-400">{{ c.type }}</span>
                </span>
              </div>
            </li>
          </ul>
        </div>

        <!-- Agents -->
        <div v-if="expanded && agents.length" class="border-t border-gray-100 dark:border-gray-800 px-3 py-2">
          <div class="text-[10px] uppercase tracking-wide text-gray-400 mb-1">
            {{ phase === 'created' ? $t('tools.createDemoDataset.agentsCreated') : $t('tools.createDemoDataset.agents') }}
          </div>
          <ul class="space-y-1">
            <li
              v-for="a in agents" :key="a.name"
              class="flex items-start gap-2 text-[11px] rounded-md px-1.5 py-1 -mx-1.5"
              :class="phase === 'review' ? 'hover:bg-gray-50 dark:hover:bg-gray-800/60 cursor-pointer' : ''"
              :data-testid="`demo-agent-${a.name}`"
              @click="phase === 'review' && toggleAgent(a.name)"
            >
              <input
                v-if="phase === 'review'"
                type="checkbox"
                class="mt-0.5 h-3 w-3 rounded border-gray-300 text-blue-600 focus:ring-0 flex-shrink-0"
                :checked="isChecked(a.name)"
                :disabled="!canCreateAgents"
                @click.stop
                @change="toggleAgent(a.name)"
              />
              <span class="w-4 text-center text-sm leading-4 flex-shrink-0">
                <template v-if="a.emoji">{{ a.emoji }}</template>
                <Icon v-else name="heroicons-cpu-chip" class="w-3.5 h-3.5 text-blue-500" />
              </span>
              <span class="min-w-0 flex-1" :class="phase !== 'review' || isChecked(a.name) ? '' : 'opacity-50'">
                <span class="flex items-center gap-1.5">
                  <span class="font-medium text-gray-800 dark:text-gray-200 truncate" dir="auto">{{ a.name }}</span>
                  <span v-if="a.skipped" class="text-[9px] px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400">
                    {{ $t('tools.createDemoDataset.skipped') }}
                  </span>
                </span>
                <span v-if="a.description" class="block text-gray-500 dark:text-gray-400 leading-snug" dir="auto">{{ a.description }}</span>
                <span class="mt-0.5 flex flex-wrap gap-1">
                  <span
                    v-for="tn in a.tables" :key="tn"
                    class="text-[10px] px-1.5 py-0 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 font-mono"
                  >{{ tn }}</span>
                </span>
              </span>
              <NuxtLink
                v-if="a.id"
                :to="`/agents/${a.id}`"
                class="text-[11px] text-blue-600 hover:text-blue-800 dark:text-blue-400 inline-flex items-center gap-1 flex-shrink-0"
                @click.stop
              >
                <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
                {{ $t('tools.createDemoDataset.open') }}
              </NuxtLink>
            </li>
          </ul>
          <div v-if="phase === 'review' && !canCreateAgents" class="mt-1 text-[10px] text-amber-600 dark:text-amber-400">
            {{ $t('tools.createDemoDataset.noAgentPermission') }}
          </div>
        </div>

        <!-- Review actions -->
        <div v-if="phase === 'review'" class="border-t border-gray-100 dark:border-gray-800 px-3 py-2" data-testid="demo-dataset-review">
          <div v-if="showFeedback" class="mb-2">
            <textarea
              v-model="feedbackDraft"
              rows="2"
              :placeholder="$t('tools.createDemoDataset.feedbackPlaceholder')"
              class="w-full text-xs rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400 resize-none"
              data-testid="demo-dataset-feedback"
            ></textarea>
          </div>
          <div class="flex items-center gap-2">
            <span class="text-[10px] text-gray-400 truncate">
              {{ $t('tools.createDemoDataset.generatedBy', { model: confirmation?.model || '' }) }}
            </span>
            <span class="flex-1"></span>
            <template v-if="!showFeedback">
              <button
                class="px-2.5 py-1 text-xs rounded-md text-gray-500 hover:text-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                :disabled="responding"
                data-testid="demo-dataset-request-changes"
                @click="showFeedback = true"
              >{{ $t('tools.createDemoDataset.requestChanges') }}</button>
              <button
                class="px-2.5 py-1 text-xs font-medium rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1"
                :disabled="responding"
                data-testid="demo-dataset-approve"
                @click="respond(true)"
              >
                <Spinner v-if="responding && pendingChoice === true" class="w-3 h-3" />
                {{ approveLabel }}
              </button>
            </template>
            <template v-else>
              <button
                class="px-2.5 py-1 text-xs rounded-md text-gray-500 hover:text-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800"
                :disabled="responding"
                @click="showFeedback = false"
              >{{ $t('tools.createDemoDataset.cancel') }}</button>
              <button
                class="px-2.5 py-1 text-xs font-medium rounded-md bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 hover:opacity-90 disabled:opacity-50 inline-flex items-center gap-1"
                :disabled="responding"
                data-testid="demo-dataset-send-feedback"
                @click="respond(false)"
              >
                <Spinner v-if="responding && pendingChoice === false" class="w-3 h-3" />
                {{ $t('tools.createDemoDataset.sendFeedback') }}
              </button>
            </template>
          </div>
          <div v-if="respondError" class="mt-1 text-[10px] text-red-500">{{ respondError }}</div>
        </div>

        <!-- Result footer -->
        <div v-if="phase === 'created'" class="border-t border-gray-100 dark:border-gray-800 px-3 py-1.5 flex items-center gap-2 text-[10px] text-gray-400">
          <Icon name="heroicons-circle-stack" class="w-3 h-3" />
          <span>{{ formatRows(output.total_rows) }} {{ $t('tools.createDemoDataset.rows') }}</span>
          <span>·</span>
          <bdi>{{ formatBytes(output.file_size_bytes) }}</bdi>
          <span v-if="output.seconds">·</span>
          <span v-if="output.seconds">{{ Math.round(output.seconds) }}s</span>
          <span v-if="output.model" class="truncate">· <bdi>{{ output.model }}</bdi></span>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'

const { t, locale } = useI18n()

const props = defineProps<{
  toolExecution: {
    status: string
    arguments_json?: any
    result_json?: any
    confirmation?: any
    progress_stage?: string
    demo_progress?: any
  }
  systemCompletionId?: string
}>()

const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const status = computed<string>(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
// The tool's output is either the result_json itself or nested under `output`.
const output = computed<any>(() => (result.value?.output && typeof result.value.output === 'object') ? result.value.output : result.value)
const outputStatus = computed<string>(() => output.value?.status || '')
const message = computed<string>(() => output.value?.message || '')
const errors = computed<string[]>(() => Array.isArray(output.value?.errors) ? output.value.errors : [])
const feedback = computed<string>(() => output.value?.feedback || '')
const confirmation = computed<any>(() => props.toolExecution?.confirmation || null)
const progressStage = computed<string>(() => props.toolExecution?.progress_stage || '')
const progress = computed<any>(() => props.toolExecution?.demo_progress || {})

const responded = ref(false)
const responding = ref(false)
const pendingChoice = ref<boolean | null>(null)
const respondError = ref('')
const showFeedback = ref(false)
const feedbackDraft = ref('')
const openTable = ref<string | null>(null)

// A declined or failed proposal collapses to its header so the revised one
// that follows stands out.
const expanded = computed(() => !['rejected', 'failed'].includes(phase.value))

const phase = computed<'designing' | 'review' | 'working' | 'created' | 'rejected' | 'failed'>(() => {
  if (status.value === 'running') {
    if (!responded.value && confirmation.value?.confirmation_id &&
        ['awaiting_confirmation', 'awaiting_approval'].includes(progressStage.value)) return 'review'
    if (['generating', 'writing', 'connecting', 'creating_agents'].includes(progressStage.value) || responded.value) return 'working'
    return 'designing'
  }
  if (output.value?.success) return 'created'
  if (['rejected', 'timed_out'].includes(outputStatus.value)) return 'rejected'
  return 'failed'
})

const workingLabel = computed(() => {
  const stage = progressStage.value
  if (stage === 'writing') return t('tools.createDemoDataset.writing')
  if (stage === 'connecting') return t('tools.createDemoDataset.connecting')
  if (stage === 'creating_agents') return t('tools.createDemoDataset.creatingAgents')
  return t('tools.createDemoDataset.generating')
})

const tables = computed<any[]>(() => Array.isArray(args.value?.tables) ? args.value.tables : [])
const progressTables = computed<Record<string, any>>(() => {
  const out: Record<string, any> = {}
  for (const row of (progress.value?.tables || [])) out[row.name] = row
  return out
})
const progressDone = computed<number>(() => Number(progress.value?.done || 0))
const progressTotal = computed<number>(() => Number(progress.value?.total || 0))
const resultTables = computed<Record<string, any>>(() => {
  const out: Record<string, any> = {}
  for (const row of (output.value?.tables || [])) out[row.name] = row
  return out
})
function tableState(name: string): any {
  if (phase.value === 'created') return { state: 'done' }
  if (phase.value !== 'working') return { state: 'idle' }
  return progressTables.value[name] || { state: 'queued' }
}
function tableRows(tb: any): number {
  return resultTables.value[tb.name]?.rows ?? progressTables.value[tb.name]?.rows ?? tb.row_count ?? 0
}
const rowsTotal = computed<number>(() => tables.value.reduce((s, tb) => s + (tableRows(tb) || 0), 0))
function toggleTable(name: string) { openTable.value = openTable.value === name ? null : name }

function emojiOf(icon?: string | null): string {
  if (!icon) return ''
  const s = String(icon).replace(/^emoji:/, '').trim()
  return /[^\x00-\x7F]/.test(s) ? s : ''
}
const datasetEmoji = computed(() => emojiOf(args.value?.icon))
const connectionName = computed<string>(() => output.value?.connection_name || '')

const dateRange = computed<string>(() => {
  const a = args.value?.date_range_start, b = args.value?.date_range_end
  if (!a || !b) return ''
  const fmt = (d: string) => {
    const dt = new Date(`${d}T00:00:00`)
    return isNaN(dt.getTime()) ? d : new Intl.DateTimeFormat(locale.value, { month: 'short', year: 'numeric' }).format(dt)
  }
  return `${fmt(a)} – ${fmt(b)}`
})

// Agents: the spec's suggestions, overlaid with what was actually created.
const checked = ref<Record<string, boolean>>({})
watch(() => args.value?.agents, (list: any[]) => {
  const next: Record<string, boolean> = {}
  for (const a of (list || [])) next[a.name] = checked.value[a.name] ?? (a.selected !== false)
  checked.value = next
}, { immediate: true })
const canCreateAgents = computed<boolean>(() => confirmation.value?.can_create_agents !== false)
function isChecked(name: string) { return canCreateAgents.value && !!checked.value[name] }
function toggleAgent(name: string) {
  if (!canCreateAgents.value) return
  checked.value = { ...checked.value, [name]: !checked.value[name] }
}
const createdByName = computed<Record<string, any>>(() => {
  const out: Record<string, any> = {}
  for (const a of (output.value?.agents || [])) out[(a.requested_name || a.name || '').toLowerCase()] = a
  return out
})
const skippedNames = computed<Set<string>>(() => new Set((output.value?.skipped_agents || []).map((s: any) => (s.name || '').toLowerCase())))
const agents = computed<any[]>(() => {
  const list: any[] = Array.isArray(args.value?.agents) ? args.value.agents : []
  if (phase.value === 'created') {
    return list
      .filter(a => createdByName.value[a.name.toLowerCase()] || skippedNames.value.has(a.name.toLowerCase()))
      .map(a => {
        const c = createdByName.value[a.name.toLowerCase()]
        return {
          ...a,
          id: c?.data_source_id,
          name: c?.name || a.name,
          emoji: emojiOf(c?.icon) || emojiOf(a.icon),
          tables: c?.active_tables?.length ? c.active_tables : a.tables,
          skipped: !c,
        }
      })
  }
  const all = list.map(a => ({ ...a, emoji: emojiOf(a.icon) }))
  if (phase.value === 'working') {
    const chosen: string[] | null = Array.isArray(progress.value?.agents) ? progress.value.agents
      : (responded.value ? selectedNames.value : null)
    if (chosen) return all.filter(a => chosen.includes(a.name))
  }
  return all
})
const selectedNames = computed<string[]>(() =>
  (Array.isArray(args.value?.agents) ? args.value.agents : []).filter((a: any) => isChecked(a.name)).map((a: any) => a.name))
const approveLabel = computed(() => {
  const n = selectedNames.value.length
  return n ? t('tools.createDemoDataset.approveWithAgents', { n }, n) : t('tools.createDemoDataset.approve')
})

// Created agents: let the page refresh its agent selector.
const notified = ref(false)
watch(() => output.value?.agents, (v: any[]) => {
  if (Array.isArray(v) && v.length && !notified.value) {
    notified.value = true
    try { window.dispatchEvent(new CustomEvent('report:mutated', { detail: { kind: 'data_sources' } })) } catch {}
  }
}, { immediate: false })

const { markConfirmationAnswered } = useToolConfirmations()

async function respond(approved: boolean) {
  const cid = confirmation.value?.confirmation_id
  if (!cid || !props.systemCompletionId || responding.value) return
  responding.value = true
  pendingChoice.value = approved
  respondError.value = ''
  try {
    const { error } = await useMyFetch(`/completions/${props.systemCompletionId}/mcp_tool_confirmations/${cid}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        approved,
        remember: false,
        response: approved
          ? { agents: selectedNames.value }
          : { feedback: feedbackDraft.value.trim() },
      }),
    })
    if (error?.value) {
      const code = (error.value as any)?.statusCode
      respondError.value = code === 410 ? t('tools.createDemoDataset.expired') : t('tools.createDemoDataset.respondFailed')
      return
    }
    responded.value = true
    markConfirmationAnswered(cid)
  } catch (e) {
    respondError.value = t('tools.createDemoDataset.respondFailed')
  } finally {
    responding.value = false
    pendingChoice.value = null
  }
}

function formatRows(n: number): string {
  if (!n) return '0'
  return new Intl.NumberFormat(locale.value, { notation: n >= 10_000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(n)
}
function formatBytes(n: number): string {
  if (!n) return '0 KB'
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(n / 1024))} KB`
}
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer { 0% { background-position: 0% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
