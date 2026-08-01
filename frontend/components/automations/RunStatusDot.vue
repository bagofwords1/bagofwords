<template>
  <UTooltip v-if="visual" :text="visual.label">
    <Spinner v-if="visual.kind === 'spinner'" class="w-3 h-3 shrink-0 text-blue-500" />
    <span v-else class="w-2 h-2 rounded-full shrink-0 inline-block" :class="visual.cls"></span>
  </UTooltip>
</template>

<script setup lang="ts">
// Per-run verdict for automation history lists (scheduled tasks, triggers).
//
// Deliberately NOT ReportStatusDot: that badge answers "is there something
// here for ME" (unread, viewer-relative) and renders nothing once a report
// has been read — which would leave a failed run looking identical to a
// successful one the moment you open it. A run ledger needs the opposite: a
// stable verdict that stays put.
//
// Live activity still wins while a run is in flight, so a history list open
// during a fire shows the spinner instead of a stale outcome.
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{
  reportId: string
  // Last system-turn status from the runs API: success | error | in_progress.
  status?: string | null
}>()

const { t } = useI18n()
const { activityFor } = useReportActivity()

const visual = computed(() => {
  const live = activityFor(props.reportId)
  if (live?.state === 'running' || live?.state === 'queued') {
    return { kind: 'spinner', cls: '', label: t('scheduledPrompt.runStatus.running') }
  }
  if (live?.state === 'awaiting_user') {
    return { kind: 'dot', cls: 'bg-amber-500 animate-pulse', label: t('scheduledPrompt.runStatus.waiting') }
  }
  if (props.status === 'in_progress') {
    return { kind: 'spinner', cls: '', label: t('scheduledPrompt.runStatus.running') }
  }
  if (props.status === 'error') {
    return { kind: 'dot', cls: 'bg-red-500', label: t('scheduledPrompt.runStatus.failed') }
  }
  if (props.status === 'success') {
    return { kind: 'dot', cls: 'bg-green-500', label: t('scheduledPrompt.runStatus.succeeded') }
  }
  // No system turn at all — the run never answered. Neutral rather than green:
  // claiming success for a run that produced nothing is the exact failure this
  // whole surface exists to stop.
  return { kind: 'dot', cls: 'bg-gray-300 dark:bg-gray-600', label: t('scheduledPrompt.runStatus.noResponse') }
})
</script>
