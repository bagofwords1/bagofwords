<template>
  <div class="min-w-0" data-testid="remote-shell-tool">
    <UButton variant="ghost" color="gray" class="no-press !p-0 w-full justify-start gap-1.5 text-chat-2 font-normal text-ink-muted" :aria-expanded="expanded" @click="expanded = !expanded">
      <Icon name="heroicons-command-line" class="w-3.5 h-3.5 shrink-0" data-testid="shell-icon" />
      <span dir="ltr" class="truncate font-mono text-start min-w-0" :title="args.command">{{ args.command || t('tools.shell.command') }}</span>
      <span class="shrink-0" :class="{ 'text-amber-600 dark:text-amber-500': ['failed', 'execution_timed_out'].includes(outcome) }">{{ t(`tools.shell.${outcome}`) }}</span>
      <span v-if="duration" class="shrink-0 nums">· {{ duration }}</span>
      <Spinner v-if="outcome === 'running'" class="w-3 h-3 shrink-0" />
      <Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 shrink-0 rtl-flip" />
    </UButton>
    <div v-if="expanded" class="mt-2 space-y-2 rounded-lg border border-line bg-surface-subtle p-3 text-chat-2" data-testid="shell-details">
      <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-ink-muted">
        <dt>{{ t('tools.shell.server') }}</dt><dd class="min-w-0 break-words" dir="ltr">{{ target.server || connectionName || '—' }}<template v-if="target.port">:{{ target.port }}</template></dd>
        <dt>{{ t('tools.shell.directory') }}</dt><dd class="min-w-0 break-words font-mono" dir="ltr">{{ args.working_directory || '—' }}</dd>
      </dl>
      <pre dir="ltr" class="m-0 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-surface p-2 font-mono text-ink">{{ args.command }}</pre>
      <div v-if="approvalLabel" class="text-ink-muted">{{ approvalLabel }}</div>
      <p v-if="outcome === 'execution_timed_out' || (outcome === 'cancelled' && result.execution_state !== 'not_started')" class="text-ink-muted">{{ t('tools.shell.unknownState') }}</p>
      <div v-if="errorMessage" class="text-amber-600 dark:text-amber-500 break-words" data-testid="shell-error">{{ errorMessage }}</div>
      <template v-if="executionResult">
        <div v-if="executionResult.exit_code != null" class="text-ink-muted">{{ t('tools.shell.exitCode', { code: executionResult.exit_code }) }}</div>
        <template v-for="stream in ['stdout', 'stderr']" :key="stream">
          <div v-if="executionResult[stream]">
            <div class="mb-1 text-ink-muted">{{ stream }}</div>
            <pre dir="ltr" class="m-0 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-surface p-2 font-mono text-ink">{{ executionResult[stream] }}</pre>
          </div>
        </template>
      </template>
      <pre v-else-if="result.preview" dir="ltr" class="m-0 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-surface p-2 font-mono text-ink">{{ typeof result.preview === 'string' ? result.preview : JSON.stringify(result.preview, null, 2) }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
import Spinner from '~/components/Spinner.vue'
import { toolOutcome } from '~/utils/toolOutcome'
const props = defineProps<{
  args: Record<string, any>
  result: Record<string, any>
  status: string
  progressStage?: string
  runStatus?: string
  duration?: string
  connectionName?: string
  approvalLabel?: string
  confirmationTarget?: Record<string, any>
}>()
const { t } = useResponseI18n()
const expanded = ref(false)
const outcome = computed(() => toolOutcome(props.result, props.status, props.progressStage, props.runStatus))
const target = computed(() => props.result.execution_target || props.confirmationTarget || {})
const executionResult = computed(() => {
  const r = props.result.execution_result || props.result.preview
  return r && typeof r === 'object' && ('stdout' in r || 'stderr' in r || 'exit_code' in r) ? r : null
})
const errorMessage = computed(() => {
  // Planner guidance on historical approvals is not customer-facing detail.
  if (['cancelled', 'denied', 'approval_expired'].includes(outcome.value)) return t(outcome.value === 'cancelled' && props.result.execution_state === 'not_started' ? 'tools.shell.cancelledBeforeExecution' : `tools.shell.${outcome.value}`)
  return props.result.error_message || props.result.error || ''
})
</script>
