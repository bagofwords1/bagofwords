<template>
  <UTooltip v-if="visual" :text="visual.label">
    <Spinner v-if="visual.kind === 'spinner'" class="w-2 h-2 shrink-0 text-blue-500" />
    <span v-else class="w-1.5 h-1.5 rounded-full shrink-0 inline-block" :class="visual.cls"></span>
  </UTooltip>
  <!-- Idle / no-activity. Opt-in (show-idle) so surfaces that render the dot in
       a fixed status column — the sidebar — keep that column uniform with a
       hollow ring. Off by default: inline badges elsewhere still show nothing
       when there's nothing to say. -->
  <span
    v-else-if="showIdle"
    class="w-1.5 h-1.5 rounded-full shrink-0 inline-block border border-gray-300 dark:border-gray-600"
    aria-hidden="true"
  ></span>
</template>

<script setup lang="ts">
// Minimal per-report status badge, shared by every report list surface.
// Renders nothing when there is nothing to say (done + read = no badge),
// unless show-idle is set, in which case idle shows a hollow ring:
//   awaiting_user        -> pulsing amber dot ("Waiting for your input")
//   running / queued     -> animated spinner
//   error + unread       -> amber dot
//   unread               -> blue dot
//   idle (show-idle only) -> hollow gray ring
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{ reportId: string; showIdle?: boolean }>()
const { t } = useI18n()
const { activityFor } = useReportActivity()

const visual = computed(() => {
  const a = activityFor(props.reportId)
  if (!a) return null
  if (a.state === 'awaiting_user') {
    return { kind: 'dot', cls: 'bg-amber-500 animate-pulse', label: t('reports.status.waitingForYou') }
  }
  if (a.state === 'running' || a.state === 'queued') {
    return { kind: 'spinner', cls: '', label: t('reports.status.running') }
  }
  if (a.error && a.unread) {
    return { kind: 'dot', cls: 'bg-amber-500', label: t('reports.status.errorNew') }
  }
  if (a.unread) {
    return { kind: 'dot', cls: 'bg-blue-500', label: t('reports.status.newActivity') }
  }
  return null
})
</script>
