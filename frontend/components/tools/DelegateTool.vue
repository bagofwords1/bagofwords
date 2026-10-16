<template>
  <div class="mt-1 mb-1" :data-testid="`delegate-card-${subAgentId || 'refused'}`" dir="auto">
    <!-- Header line -->
    <div
      class="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400 min-w-0"
      :class="{ 'cursor-pointer select-none': canExpand }"
      @click="canExpand && (expanded = !expanded)"
    >
      <Spinner v-if="isLive" class="w-3 h-3 flex-shrink-0 text-gray-400" />
      <Icon v-else :name="stateIcon" class="w-3.5 h-3.5 flex-shrink-0" :class="stateIconClass" />
      <span class="font-medium text-gray-700 dark:text-gray-300 truncate" :class="isLive ? 'tool-shimmer' : ''">{{ title }}</span>

      <!-- Outcome badge -->
      <span v-if="outcome" class="px-1.5 py-0.5 rounded text-[10px] font-medium flex-shrink-0" :class="outcomeClass">
        {{ t(`tools.delegate.outcomes.${outcome}`, outcome) }}<span v-if="confidence !== null"> · {{ Math.round(confidence * 100) }}%</span>
      </span>
      <span v-else-if="refused" class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 dark:bg-gray-800 text-gray-500 flex-shrink-0">{{ t('tools.delegate.refused') }}</span>
      <span v-else class="text-[10px] text-gray-400 flex-shrink-0">{{ t(`tools.delegate.status.${statusKey}`, status) }}</span>

      <!-- Counters -->
      <span class="text-[10px] text-gray-400 flex-shrink-0 tabular-nums" dir="ltr">
        <template v-if="stepsLabel">{{ stepsLabel }}</template>
        <template v-if="elapsedLabel"> · {{ elapsedLabel }}</template>
        <template v-if="tokensLabel"> · {{ tokensLabel }}</template>
      </span>

      <Icon v-if="canExpand" name="heroicons-chevron-down" class="w-3 h-3 ms-0.5 text-gray-400 transition-transform duration-200" :class="{ '-rotate-90': !expanded }" />

      <!-- Stop (only while live, only when interactive) -->
      <button
        v-if="isLive && !readonly"
        class="ms-auto inline-flex items-center gap-0.5 text-[10px] text-red-600 hover:text-red-800 flex-shrink-0"
        :disabled="stopping"
        :title="t('tools.delegate.stop')"
        data-testid="delegate-stop"
        @click.stop="stop"
      >
        <Icon name="heroicons-stop" class="w-3 h-3" />
        <span>{{ stopping ? t('tools.delegate.stopping') : t('tools.delegate.stop') }}</span>
      </button>
    </div>

    <!-- Refused: say why -->
    <p v-if="refused" class="ms-5 mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">{{ refused }}</p>

    <!-- Body -->
    <div v-if="expanded && canExpand" class="ms-5 mt-1.5 space-y-2 text-xs text-gray-600 dark:text-gray-400">
      <!-- Goal -->
      <p class="text-[11px] text-gray-500 dark:text-gray-500 line-clamp-3" :title="goal">{{ goal }}</p>

      <!-- Live: latest tool calls -->
      <ul v-if="isLive && calls.length" class="space-y-0.5" data-testid="delegate-steps">
        <li v-for="(c, i) in calls.slice(-6)" :key="i" class="flex items-center gap-1 text-[11px]">
          <Spinner v-if="c.status === 'in_progress' || c.status === 'running'" class="w-2.5 h-2.5 text-blue-400 flex-shrink-0" />
          <Icon v-else :name="c.status === 'success' ? 'heroicons-check' : 'heroicons-x-mark'" class="w-3 h-3 flex-shrink-0" :class="c.status === 'success' ? 'text-green-500' : 'text-gray-400'" />
          <span class="truncate" :title="c.summary || c.title">{{ c.title }}</span>
          <span v-if="c.duration_ms" class="text-[10px] text-gray-400 flex-shrink-0 tabular-nums">{{ Math.round(c.duration_ms / 1000) }}s</span>
        </li>
      </ul>
      <p v-else-if="isLive" class="text-[11px] text-gray-400">{{ t('tools.delegate.noCallsYet') }}</p>

      <!-- Finished: the record -->
      <template v-if="record">
        <p v-if="record.summary" class="text-gray-700 dark:text-gray-300">{{ record.summary }}</p>
        <div v-if="record.findings?.length">
          <div class="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">{{ t('tools.delegate.findings') }}</div>
          <ul class="list-disc ms-4 space-y-0.5">
            <li v-for="(f, i) in record.findings" :key="i">{{ f.claim }}<span v-if="f.magnitude" class="text-gray-400"> ({{ f.magnitude }})</span></li>
          </ul>
        </div>
        <div v-if="record.ruled_out?.length">
          <div class="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">{{ t('tools.delegate.ruledOut') }}</div>
          <ul class="list-disc ms-4 space-y-0.5"><li v-for="(r, i) in record.ruled_out" :key="i">{{ r }}</li></ul>
        </div>
        <div v-if="record.caveats?.length">
          <div class="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">{{ t('tools.delegate.caveats') }}</div>
          <ul class="list-disc ms-4 space-y-0.5"><li v-for="(c, i) in record.caveats" :key="i">{{ c }}</li></ul>
        </div>
        <p v-if="record.unknown" class="text-amber-700 dark:text-amber-400"><span class="font-medium">{{ t('tools.delegate.unknown') }}:</span> {{ record.unknown }}</p>
        <div v-if="record.evidence?.length" class="flex flex-wrap gap-1">
          <span v-for="(e, i) in record.evidence" :key="i" class="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-[10px] text-gray-500" :title="e.id">{{ e.kind }}<span v-if="e.label">: {{ e.label }}</span></span>
        </div>
        <p v-if="record.synthesized" class="text-[10px] text-gray-400 italic">{{ t('tools.delegate.synthesized') }}</p>
      </template>

      <div v-if="detail?.messages?.length" class="space-y-2" data-testid="sub-agent-message-history">
        <div class="text-[10px] uppercase tracking-wide text-gray-400">{{ t('tools.messageSubAgent.messages') }}</div>
        <div v-for="message in detail.messages" :key="message.message_id" class="rounded-md border border-gray-200 dark:border-gray-700 p-2">
          <div class="flex flex-wrap items-center gap-1 text-[10px] text-gray-500">
            <span>{{ message.sender === 'parent' ? t('tools.messageSubAgent.parent') : title }}</span>
            <span aria-hidden="true" class="inline-block rtl:rotate-180">→</span>
            <span>{{ message.recipient === 'parent' ? t('tools.messageSubAgent.parent') : title }}</span>
            <span class="ms-auto">{{ t(`tools.messageSubAgent.states.${message.status}`) }}</span>
          </div>
          <p class="mt-1 whitespace-pre-wrap break-words" dir="auto">{{ message.body }}</p>
        </div>
      </div>

      <!-- Envelope line -->
      <div v-if="envelope" class="text-[10px] text-gray-400 flex flex-wrap gap-x-2">
        <span v-if="envelope.exit_reason">{{ t('tools.delegate.exit') }}: {{ envelope.exit_reason }}</span>
        <span v-if="envelope.model">{{ envelope.model }}</span>
        <span v-if="noteId" class="cursor-pointer hover:text-blue-600" @click.stop="emit('openNote', noteId)">{{ t('tools.delegate.openNote') }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Spinner from '~/components/Spinner.vue'
import { subAgentStatusKey } from '~/utils/subAgentStatus'

const { t } = useI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  arguments_json?: any
  result_json?: any
  started_at?: string
}

const props = defineProps<{
  toolExecution: ToolExecution
  readonly?: boolean
  reportId?: string
  canExpand?: boolean
}>()
const emit = defineEmits<{ (e: 'openNote', noteId: string): void }>()

// The report page polls GET /reports/{id}/sub-agents while any wave is live and
// provides the rows by id; the card reads them so N cards cost one poll.
const subAgents = inject<any>('subAgents', null)

const expanded = ref(false)
const stopping = ref(false)
const detail = ref<any>(null)
const now = ref(Date.now())
let tick: ReturnType<typeof setInterval> | null = null
let detailTimer: ReturnType<typeof setInterval> | null = null

const rj = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const subAgentId = computed<string | null>(() => rj.value?.sub_agent_id || null)
const refused = computed<string | null>(() => (rj.value?.status === 'refused' ? (rj.value?.refused || t('tools.delegate.refused')) : null))
const live = computed<any>(() => (subAgentId.value && subAgents?.byId?.value?.[subAgentId.value]) || null)
const title = computed(() => live.value?.title || rj.value?.title || args.value?.title || t('tools.delegate.label'))
const goal = computed(() => live.value?.goal || detail.value?.goal || args.value?.goal || '')
const status = computed<string>(() => {
  if (refused.value) return 'refused'
  return detail.value?.status || live.value?.status || rj.value?.status || (props.toolExecution?.status === 'in_progress' ? 'running' : 'running')
})
const isLive = computed(() => !refused.value && (status.value === 'running' || status.value === 'pending' || status.value === 'waiting'))
// Same precedence as `status`: the freshest source decides whether a waiting
// child is blocked on the parent or merely on a free slot.
const statusKey = computed(() => subAgentStatusKey({
  status: status.value,
  waiting_for: detail.value?.waiting_for ?? live.value?.waiting_for ?? rj.value?.waiting_for,
}))
const record = computed<any>(() => detail.value?.record || live.value?.record || rj.value?.record || null)
const outcome = computed<string | null>(() => record.value?.outcome || live.value?.outcome || null)
const confidence = computed<number | null>(() => {
  const c = record.value?.confidence ?? live.value?.confidence
  return typeof c === 'number' ? c : null
})
const envelope = computed<any>(() => detail.value?.envelope || rj.value?.envelope || null)
const noteId = computed<string | null>(() => record.value?.report_note_id || rj.value?.report_note_id || live.value?.report_note_id || null)
const calls = computed<any[]>(() => detail.value?.calls || [])
const canExpand = computed(() => !refused.value)

const stepsUsed = computed<number | null>(() => {
  const v = detail.value?.steps_used ?? live.value?.steps_used ?? rj.value?.steps_used ?? envelope.value?.steps_used
  return typeof v === 'number' ? v : null
})
const maxSteps = computed<number | null>(() => live.value?.max_steps ?? detail.value?.max_steps ?? rj.value?.max_steps ?? envelope.value?.max_steps ?? null)
const stepsLabel = computed(() => {
  if (stepsUsed.value === null) return ''
  return isLive.value && maxSteps.value ? `${stepsUsed.value}/${maxSteps.value} ${t('tools.delegate.steps')}` : `${stepsUsed.value} ${t('tools.delegate.steps')}`
})
const tokensLabel = computed(() => {
  const tk = live.value?.tokens || envelope.value?.tokens || rj.value?.envelope?.tokens
  const total = tk?.total_tokens ?? ((tk?.prompt_tokens || 0) + (tk?.completion_tokens || 0))
  if (!total) return ''
  return total >= 1000 ? `${(total / 1000).toFixed(total >= 10000 ? 0 : 1)}k tok` : `${total} tok`
})

function parseServerTimestamp(v: any): number | null {
  if (!v) return null
  const s = String(v)
  const ts = Date.parse(/Z|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + 'Z')
  return Number.isNaN(ts) ? null : ts
}
const elapsedLabel = computed(() => {
  const start = parseServerTimestamp(live.value?.started_at || detail.value?.started_at)
  if (!start) return ''
  const end = isLive.value ? now.value : (parseServerTimestamp(live.value?.finished_at || detail.value?.finished_at) || now.value)
  const s = Math.max(0, Math.floor((end - start) / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
})

const stateIcon = computed(() => {
  if (refused.value) return 'heroicons-no-symbol'
  switch (status.value) {
    case 'finished': return outcome.value === 'refuted' ? 'heroicons-x-circle' : 'heroicons-check-circle'
    case 'failed': return 'heroicons-exclamation-circle'
    case 'timed_out': return 'heroicons-clock'
    case 'interrupted': return 'heroicons-stop-circle'
    default: return 'heroicons-arrow-path'
  }
})
const stateIconClass = computed(() => {
  switch (status.value) {
    case 'finished': return outcome.value === 'refuted' ? 'text-gray-400' : 'text-green-500'
    case 'failed': return 'text-red-400'
    case 'timed_out': return 'text-amber-500'
    default: return 'text-gray-400'
  }
})
const outcomeClass = computed(() => {
  switch (outcome.value) {
    case 'supported':
    case 'done': return 'bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300'
    case 'refuted': return 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'
    case 'inconclusive':
    case 'partial': return 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
    case 'blocked': return 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300'
    default: return 'bg-gray-100 text-gray-600'
  }
})

async function fetchDetail() {
  if (!props.reportId || !subAgentId.value || props.readonly) return
  try {
    const { data } = await useMyFetch(`/reports/${props.reportId}/sub-agents/${subAgentId.value}`)
    if (data.value) detail.value = data.value
  } catch { /* keep the last snapshot */ }
}
async function stop() {
  if (!props.reportId || !subAgentId.value) return
  stopping.value = true
  try {
    await useMyFetch(`/reports/${props.reportId}/sub-agents/${subAgentId.value}/stop`, { method: 'POST' })
    await fetchDetail()
    subAgents?.refresh?.()
  } catch (e) {
    console.error('Failed to stop sub-agent', e)
  } finally {
    stopping.value = false
  }
}

function syncDetailPolling() {
  if (detailTimer) { clearInterval(detailTimer); detailTimer = null }
  if (expanded.value && !props.readonly) {
    // A finished card already has its result in the wave snapshot, but its
    // durable message history is only returned by the detail endpoint.
    fetchDetail()
    if (isLive.value) detailTimer = setInterval(fetchDetail, 3000)
  }
}
watch([expanded, isLive], syncDetailPolling)
// When the run ends, pull the final record once (envelope + calls).
watch(isLive, (v, prev) => { if (prev && !v) fetchDetail() })

onMounted(() => {
  tick = setInterval(() => { now.value = Date.now() }, 1000)
  if (!isLive.value && subAgentId.value && !record.value && !props.readonly) fetchDetail()
})
onBeforeUnmount(() => {
  if (tick) clearInterval(tick)
  if (detailTimer) clearInterval(detailTimer)
})
</script>
