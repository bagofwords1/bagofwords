<template>
  <div class="min-w-0" data-testid="sub-agent-message">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-chat-bubble-left-right" :running="sending" :error="!sending && failed" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>{{ sending ? t('tools.messageSubAgent.sending') : t(failed ? 'tools.messageSubAgent.failed' : 'tools.messageSubAgent.sent', { title }) }}</template>
    </ToolCallHeader>
    <div v-if="toolHeaderExpanded" class="mt-2 space-y-2 text-chat-2">
      <p class="whitespace-pre-wrap break-words text-ink-2" dir="auto">{{ body }}</p>
      <p v-if="failed" class="text-ink-muted">{{ error || t('tools.messageSubAgent.failedHint') }}</p>
      <p v-else class="text-ink-muted">{{ t('tools.messageSubAgent.sentHint') }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)
import { computed, inject } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
const { t } = useResponseI18n()
const props = defineProps<{ toolExecution: { status: string; result_json?: any; arguments_json?: any; error_message?: string; result_summary?: string } }>()
const subAgents = inject<any>('subAgents', null)
const result = computed(() => props.toolExecution.result_json || {})
const args = computed(() => props.toolExecution.arguments_json || {})
const sending = computed(() => ['in_progress', 'running', 'pending'].includes(props.toolExecution.status))
const failed = computed(() => !result.value.message_id || ['error', 'failed', 'stopped'].includes(props.toolExecution.status))
const title = computed(() => (result.value.recipient || args.value.recipient) === 'parent' ? t('tools.messageSubAgent.parent') : (result.value.title || subAgents?.byId?.value?.[args.value.recipient]?.title || t('tools.messageSubAgent.agent')))
const body = computed(() => result.value.body || args.value.body || '')
const error = computed(() => props.toolExecution.error_message || result.value.error?.message || (typeof result.value.error === 'string' ? result.value.error : ''))
</script>
