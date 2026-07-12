<template>
  <div class="mt-1 mb-2" dir="auto">
    <!-- Compact pill: clock + label + live countdown, X to cancel on the right -->
    <div
      class="inline-flex items-center gap-2 ps-2.5 pe-1.5 py-1 rounded-full border text-xs transition-colors duration-150"
      :class="cancelled
        ? 'border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 text-gray-400'
        : 'border-sky-200 dark:border-sky-800 bg-sky-50/60 dark:bg-sky-950/40 text-sky-700 dark:text-sky-300'"
    >
      <Icon
        :name="cancelled ? 'heroicons-x-mark' : (elapsed ? 'heroicons-arrow-path' : 'heroicons-clock')"
        class="w-3.5 h-3.5 flex-shrink-0"
        :class="[cancelled ? 'text-gray-400' : 'text-sky-500', elapsed && !cancelled ? 'animate-spin' : '']"
      />

      <!-- Cancelled -->
      <span v-if="cancelled" class="font-medium">{{ $t('tools.wait.cancelled') }}</span>

      <!-- Elapsed → agent is resuming -->
      <span v-else-if="elapsed" class="font-medium tool-shimmer">{{ $t('tools.wait.resuming') }}</span>

      <!-- Counting down -->
      <template v-else>
        <span class="font-medium">{{ $t('tools.wait.waiting') }}</span>
        <span class="tabular-nums font-semibold tracking-tight">{{ countdown }}</span>
        <button
          type="button"
          :title="$t('tools.wait.cancel')"
          :aria-label="$t('tools.wait.cancel')"
          :disabled="cancelling"
          @click="cancel"
          class="ms-0.5 w-5 h-5 flex items-center justify-center rounded-full text-sky-400 hover:text-white hover:bg-sky-500 disabled:opacity-40 transition-colors duration-100"
        >
          <Icon name="heroicons-x-mark" class="w-3.5 h-3.5" />
        </button>
      </template>
    </div>

    <!-- The instruction that will run on resume (subtle, one line) -->
    <p v-if="reason && !cancelled" class="mt-1.5 ms-1 text-[11px] text-gray-400 dark:text-gray-500 truncate max-w-md" dir="auto">
      {{ reason }}
    </p>
  </div>
</template>

<script setup lang="ts">
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
  }>(),
  { systemCompletionId: null }
)

const toast = useToast()
const { getErrorMessage } = useErrorMessage()

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
const elapsed = computed(() => !cancelled.value && wakeAt.value > 0 && remainingMs.value <= 0)

const countdown = computed(() => {
  const total = Math.round(remainingMs.value / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`
})

onMounted(() => {
  timer = setInterval(() => { now.value = Date.now() }, 1000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })

async function cancel() {
  if (cancelling.value || cancelled.value) return
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
        // Revert the optimistic flag — the backend never confirmed the cancel,
        // so the countdown must keep running instead of showing "Cancelled".
        locallyCancelled.value = false
        toast.add({ title: 'Failed to cancel wait', description: getErrorMessage(res.error.value), color: 'red' })
      }
    }
  } finally {
    cancelling.value = false
  }
}
</script>

<style scoped>
.tool-shimmer {
  background: linear-gradient(90deg, #0ea5e9 0%, #7dd3fc 50%, #0ea5e9 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 1.8s linear infinite;
}
@keyframes shimmer {
  0% { background-position: -100% 0; }
  100% { background-position: 100% 0; }
}
</style>
