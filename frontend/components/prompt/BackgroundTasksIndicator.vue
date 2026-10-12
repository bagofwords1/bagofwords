<template>
  <UPopover :popper="{ strategy: 'fixed', placement: 'top-start' }" mode="hover" :open-delay="150">
    <button
      type="button"
      class="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 select-none"
      data-testid="background-tasks-indicator"
    >
      <Spinner v-if="runningCount > 0 && !hasWorkingLabel" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 flex-shrink-0" />
      <span v-if="hasWorkingLabel" class="text-gray-300 dark:text-gray-600">·</span>
      <Icon v-else name="heroicons-squares-2x2" class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
      <span class="tabular-nums">{{ t('subRuns.backgroundTasks', { count: runningCount }) }}</span>
    </button>
    <template #panel>
      <div class="p-2 w-80 max-w-[90vw] text-xs" data-testid="background-tasks-popover" dir="auto">
        <div v-for="wave in waves" :key="wave.id" class="mb-2 last:mb-0">
          <div class="flex items-center justify-between gap-2 mb-1">
            <span class="font-medium text-gray-700 dark:text-gray-200 truncate">{{ t('subRuns.wave', { n: wave.number }) }}<span class="text-gray-400 font-normal"> · {{ wave.label }}</span></span>
            <button
              v-if="wave.live"
              class="text-[10px] text-red-600 hover:text-red-800 flex-shrink-0"
              :data-testid="`popover-stop-wave-${wave.id}`"
              @click.stop="emit('stopWave', wave.id)"
            >{{ t('subRuns.stopAll') }}</button>
          </div>
          <ul class="space-y-1">
            <li
              v-for="task in wave.tasks"
              :key="task.sub_run_id"
              class="flex items-center gap-1.5 px-1 py-0.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer min-w-0"
              :data-testid="`popover-task-${task.sub_run_id}`"
              @click="emit('scrollToMessage', task.parent_completion_id)"
            >
              <Spinner v-if="task.status === 'running'" class="w-2.5 h-2.5 text-blue-400 flex-shrink-0" />
              <Icon v-else :name="taskIcon(task)" class="w-3 h-3 flex-shrink-0" :class="taskIconClass(task)" />
              <span class="truncate flex-1 text-gray-700 dark:text-gray-200" :title="task.title">{{ task.title }}</span>
              <span class="text-[10px] text-gray-400 tabular-nums flex-shrink-0" dir="ltr">{{ durationLabel(task) }}</span>
              <span class="text-[10px] text-gray-400 flex-shrink-0" dir="ltr">
                <template v-if="task.status === 'running' && task.max_steps">{{ task.steps_used || 0 }}/{{ task.max_steps }}</template>
                <template v-else-if="task.outcome">{{ t(`tools.delegate.outcomes.${task.outcome}`, task.outcome) }}<template v-if="typeof task.confidence === 'number'"> {{ Math.round(task.confidence * 100) }}%</template></template>
                <template v-else>{{ t(`tools.delegate.status.${task.status}`, task.status) }}</template>
              </span>
              <button
                v-if="task.status === 'running' || task.status === 'pending'"
                class="text-red-500 hover:text-red-700 flex-shrink-0"
                :title="t('tools.delegate.stop')"
                :data-testid="`popover-stop-${task.sub_run_id}`"
                @click.stop="emit('stopSubRun', task.sub_run_id)"
              >
                <Icon name="heroicons-stop" class="w-3 h-3" />
              </button>
            </li>
          </ul>
        </div>
      </div>
    </template>
  </UPopover>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Spinner from '~/components/Spinner.vue'

const { t } = useI18n()
const props = defineProps<{
  // GET /reports/{id}/sub-runs → waves (oldest first); only live waves are shown.
  waves: any[]
  hasWorkingLabel?: boolean
}>()
const emit = defineEmits<{
  (e: 'stopSubRun', id: string): void
  (e: 'stopWave', id: string): void
  (e: 'scrollToMessage', id: string): void
}>()

const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 1000) })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })

const liveStatuses = ['collecting', 'running', 'firing']
const waves = computed(() => (props.waves || []).map((w: any, i: number) => {
  const tasks = w.sub_runs || []
  const running = tasks.filter((r: any) => r.status === 'running' || r.status === 'pending').length
  return {
    id: w.id,
    number: i + 1,
    live: liveStatuses.includes(w.status),
    label: liveStatuses.includes(w.status) ? t('subRuns.running', { count: running }) : t(`subRuns.waveStatus.${w.status}`, w.status),
    tasks,
  }
}).filter((w: any) => w.live))

const runningCount = computed(() => waves.value.reduce((n: number, w: any) => n + w.tasks.filter((r: any) => r.status === 'running' || r.status === 'pending').length, 0))

function parseServerTimestamp(v: any): number | null {
  if (!v) return null
  const s = String(v)
  const ts = Date.parse(/Z|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + 'Z')
  return Number.isNaN(ts) ? null : ts
}
function durationLabel(task: any): string {
  const start = parseServerTimestamp(task.started_at)
  if (!start) return task.status === 'pending' ? t('subRuns.queued') : ''
  const end = task.status === 'running' ? now.value : (parseServerTimestamp(task.finished_at) || now.value)
  const s = Math.max(0, Math.floor((end - start) / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}
function taskIcon(task: any): string {
  switch (task.status) {
    case 'finished': return task.outcome === 'refuted' ? 'heroicons-x-circle' : 'heroicons-check-circle'
    case 'failed': return 'heroicons-exclamation-circle'
    case 'timed_out': return 'heroicons-clock'
    case 'interrupted': return 'heroicons-stop-circle'
    case 'pending': return 'heroicons-ellipsis-horizontal'
    default: return 'heroicons-arrow-path'
  }
}
function taskIconClass(task: any): string {
  switch (task.status) {
    case 'finished': return task.outcome === 'refuted' ? 'text-gray-400' : 'text-green-500'
    case 'failed': return 'text-red-400'
    case 'timed_out': return 'text-amber-500'
    default: return 'text-gray-400'
  }
}
</script>
