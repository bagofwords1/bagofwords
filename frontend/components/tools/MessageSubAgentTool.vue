<template>
  <div class="my-1 text-xs" data-testid="sub-agent-message">
    <div v-if="sending" class="flex items-center gap-1.5 text-gray-500 tool-shimmer">
      <Spinner class="w-3 h-3" />{{ t('tools.messageSubAgent.sending') }}
    </div>
    <details v-else class="group rounded-md border border-gray-200 dark:border-gray-700">
      <summary class="flex cursor-pointer list-none items-center gap-1.5 px-2.5 py-2 text-gray-600 dark:text-gray-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500">
        <Icon :name="failed ? 'heroicons-exclamation-circle' : 'heroicons-chat-bubble-left-right'" class="h-3.5 w-3.5 shrink-0" :class="failed ? 'text-amber-600' : 'text-gray-400'" />
        <span class="min-w-0 break-words">{{ t(failed ? 'tools.messageSubAgent.failed' : 'tools.messageSubAgent.sent', { title }) }}</span>
        <Icon name="heroicons-chevron-right" class="ms-auto h-3 w-3 shrink-0 transition-transform group-open:rotate-90 rtl:rotate-180" />
      </summary>
      <div class="border-t border-gray-100 px-3 py-2 dark:border-gray-800 space-y-2">
        <p class="whitespace-pre-wrap break-words text-gray-700 dark:text-gray-300" dir="auto">{{ body }}</p>
        <p v-if="failed" class="text-amber-700 dark:text-amber-400">{{ error || t('tools.messageSubAgent.failedHint') }}</p>
        <p v-else class="text-[11px] text-gray-500">{{ t('tools.messageSubAgent.sentHint') }}</p>
      </div>
    </details>
  </div>
</template>

<script setup lang="ts">
import { computed, inject } from 'vue'
import { useI18n } from 'vue-i18n'
import Spinner from '~/components/Spinner.vue'
const { t } = useI18n()
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
