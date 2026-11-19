<template>
  <div class="min-w-0" dir="auto">
    <!-- The cancel action stays reachable while the details are collapsed. -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-clock" :expanded="toolHeaderExpanded" :running="!cancelled && !readonly" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="cancelled">{{ t('tools.wait.cancelled') }}</template>
      <template v-else-if="readonly">{{ waitedMinutes ? t('tools.wait.waitedMinutes', { count: waitedMinutes }) : t('tools.wait.waited') }}</template>
      <template v-else-if="elapsed">{{ t('tools.wait.resuming') }}</template>
      <template v-else>{{ t('tools.wait.waiting') }} {{ countdown }}</template></template>
      <template #actions>
      <template v-if="cancelled"></template>
      <template v-else-if="readonly"></template>
      <template v-else-if="elapsed"></template>
      <template v-else><button
          type="button"
          :title="t('tools.wait.cancel')"
          :aria-label="t('tools.wait.cancel')"
          :disabled="cancelling"
          @click="cancel"
          class="ms-0.5 w-5 h-5 flex items-center justify-center rounded-full text-sky-400 hover:text-white hover:bg-sky-500 disabled:opacity-40 transition-colors duration-100"
        ><Icon name="heroicons-x-mark" class="w-3.5 h-3.5" /></button></template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">

    <!-- The instruction that will run on resume (subtle, one line) -->
    <p v-if="reason && !cancelled" class="mt-1.5 ms-1 text-chat-2 text-ink-muted truncate max-w-md" dir="auto">
      {{ reason }}
    </p>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

const { t } = useResponseI18n()

import { ref, computed, onMounted, onUnmounted } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  arguments_json?: { delay_minutes?: number; reason?: string }
  result_json?: {
    status?: string
    job_id?: string
    wake_at?: string
    delay_minutes?: number
    reason?: string
  } | null
}

const props = withDefaults(
  defineProps<{
    toolExecution: ToolExecution
    systemCompletionId?: string | null
    /** Transcript-only rendering (the public share page). A wait is the one
        card whose live form is actively wrong once it is history: `wake_at` is
        always in the past there, so the countdown hits zero and the card sits
        on a spinning "Resuming…" forever, for a wait that ended long ago.
        Readonly renders the settled fact instead — it waited, for how long —
        and runs no ticking timer and no cancel affordance. */
    readonly?: boolean
  }>(),
  { systemCompletionId: null, readonly: false }
)

const rj = computed(() => props.toolExecution?.result_json ?? {})
const wakeAt = computed(() => (rj.value?.wake_at ? new Date(rj.value.wake_at).getTime() : 0))
const reason = computed(() => rj.value?.reason || props.toolExecution?.arguments_json?.reason || '')

// tick drives the countdown; a locally-set flag reflects a cancel click before
// the persisted result_json round-trips back.
const now = ref(Date.now())
const locallyCancelled = ref(false)
const cancelling = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

const cancelled = computed(() => locallyCancelled.value || rj.value?.status === 'cancelled')
const remainingMs = computed(() => Math.max(0, wakeAt.value - now.value))
// `elapsed` means "the wake time just passed, the agent is resuming now" — a
// live state. In a transcript it is never true: the wait is over, not resuming.
const elapsed = computed(() =>
  !props.readonly && !cancelled.value && wakeAt.value > 0 && remainingMs.value <= 0
)

/** How long the agent waited, for the settled transcript label. */
const waitedMinutes = computed<number>(() =>
  Number(rj.value?.delay_minutes ?? props.toolExecution?.arguments_json?.delay_minutes ?? 0)
)

const countdown = computed(() => {
  const total = Math.round(remainingMs.value / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`
})

onMounted(() => {
  // Nothing counts down in a transcript.
  if (props.readonly) return
  timer = setInterval(() => { now.value = Date.now() }, 1000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })

async function cancel() {
  if (props.readonly || cancelling.value || cancelled.value) return
  cancelling.value = true
  // Optimistic: stop the countdown immediately for a snappy feel.
  locallyCancelled.value = true
  try {
    if (props.systemCompletionId) {
      const res = await useMyFetch(
        `/completions/${props.systemCompletionId}/tool_executions/${props.toolExecution.id}/cancel_wait`,
        { method: 'POST' }
      )
      if (res?.error?.value) {
        console.warn('Failed to cancel wait', res.error.value)
      }
    }
  } finally {
    cancelling.value = false
  }
}
</script>

<style scoped>
.tool-call-details { overflow-wrap: anywhere; }
</style>
