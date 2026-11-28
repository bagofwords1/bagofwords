<template>
  <!-- Same quiet approval block as the MCP 'ask' card (MCPTool.vue): the run
       is paused on this call until the person in the chat answers. -->
  <div class="mt-1.5 ms-1 space-y-1.5 border-s-2 border-amber-300/70 py-0.5 ps-2.5 text-chat-2 dark:border-amber-600/50"
    data-testid="sandbox-confirm-card">
    <div class="text-ink">{{ t(isDelete ? 'tools.sandbox.confirm.deleteTitle' : 'tools.sandbox.confirm.runTitle') }}</div>
    <div class="text-ink-muted">{{ reason }}</div>
    <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-ink-muted">
      <template v-if="confirmation.environment">
        <dt>{{ t('tools.sandbox.environment') }}</dt><dd class="min-w-0 break-words text-ink-2"><bdi>{{ confirmation.environment }}</bdi></dd>
      </template>
      <template v-if="network">
        <dt>{{ t('tools.sandbox.network') }}</dt><dd class="min-w-0 break-words text-ink-2">{{ network }}</dd>
      </template>
      <template v-if="!isDelete && confirmation.language">
        <dt>{{ t('tools.sandbox.language') }}</dt><dd class="text-ink-2">{{ languageLabel }}</dd>
      </template>
      <template v-if="packages.length">
        <dt>{{ t('tools.sandbox.packages') }}</dt><dd class="min-w-0 break-words font-mono text-ink-2" dir="ltr">{{ packages.join(' ') }}</dd>
      </template>
      <template v-if="confirmation.input_files">
        <dt>{{ t('tools.sandbox.inputs') }}</dt><dd class="text-ink-2">{{ t('tools.sandbox.inputFiles', { count: confirmation.input_files }, { plural: confirmation.input_files }) }}</dd>
      </template>
    </dl>
    <template v-if="!isDelete">
      <pre v-if="confirmation.command" dir="ltr" class="m-0 max-h-24 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-subtle p-2 font-mono text-ink" data-testid="sandbox-confirm-command">{{ confirmation.command }}</pre>
      <pre v-if="confirmation.code" dir="ltr" class="m-0 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-subtle p-2 font-mono text-ink" data-testid="sandbox-confirm-code">{{ confirmation.code }}</pre>
      <div v-if="!confirmation.code && !confirmation.command" class="text-ink-muted" data-testid="sandbox-confirm-code-hidden">{{ t('tools.sandbox.codeHidden') }}</div>
    </template>
    <div v-if="!readonly" class="flex flex-wrap items-center gap-1.5">
      <button type="button" data-testid="sandbox-confirm-allow"
        class="rounded px-2 py-0.5 font-medium text-white transition-colors disabled:opacity-50"
        :class="isDelete ? 'bg-red-600 hover:bg-red-700' : 'bg-blue-600 hover:bg-blue-700'"
        :disabled="responding" @click="respond(true)">{{ t(isDelete ? 'tools.sandbox.confirm.delete' : 'tools.sandbox.confirm.allow') }}</button>
      <button type="button" data-testid="sandbox-confirm-deny"
        class="rounded px-2 py-0.5 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-300"
        :disabled="responding" @click="respond(false)">{{ t(isDelete ? 'tools.sandbox.confirm.keep' : 'tools.sandbox.confirm.deny') }}</button>
    </div>
    <div v-if="respondError" class="flex items-start gap-1 text-amber-600 dark:text-amber-500" data-testid="sandbox-confirm-error">
      <Icon name="heroicons-exclamation-triangle" class="mt-0.5 h-3 w-3 shrink-0" />
      <span class="break-words">{{ respondError }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
import { SANDBOX_LANGUAGES, SANDBOX_NETWORK_POLICIES, sandboxConfirmReasonKey } from '~/utils/sandboxTool'

const props = defineProps<{
  /** The `tool.confirmation` payload of a create / run / delete call. */
  confirmation: Record<string, any>
  systemCompletionId?: string
  /** A view that cannot answer (the shared link): the card without buttons. */
  readonly?: boolean
}>()
const emit = defineEmits<{ (e: 'answered', approved: boolean): void }>()
const { t } = useResponseI18n()

const isDelete = computed(() => props.confirmation.action === 'delete_sandbox')
const packages = computed<string[]>(() => Array.isArray(props.confirmation.packages) ? props.confirmation.packages : [])
const network = computed(() => {
  const policy = props.confirmation.network_policy
  return SANDBOX_NETWORK_POLICIES.includes(policy) ? t(`sandbox.network.${policy}`) : (policy || '')
})
const languageLabel = computed(() => {
  const language = props.confirmation.language
  return SANDBOX_LANGUAGES.includes(language) ? t(`tools.sandbox.lang.${language}`) : language
})
// The server's reason is an English sentence; the ones this client knows are
// said in the response's language.
const reason = computed(() => {
  if (isDelete.value) return t('tools.sandbox.confirm.deleteHint')
  const key = sandboxConfirmReasonKey(props.confirmation.reason)
  return key ? t(`tools.sandbox.confirm.reason.${key}`) : (props.confirmation.reason || '')
})

const responding = ref(false)
const respondError = ref('')
const { markConfirmationAnswered } = useToolConfirmations()

// The one endpoint every tool approval is answered through (see MCPTool.vue).
async function respond(approved: boolean) {
  const id = props.confirmation.confirmation_id
  if (!id || !props.systemCompletionId || responding.value) return
  responding.value = true
  respondError.value = ''
  try {
    const res = await useMyFetch(`/completions/${props.systemCompletionId}/mcp_tool_confirmations/${id}`, {
      method: 'POST', body: { approved, remember: false },
    })
    if (res?.error?.value) {
      const err = res.error.value as any
      respondError.value = t(err?.statusCode === 410 ? 'tools.mcp.approvalExpired' : 'tools.mcp.approvalFailed')
    } else {
      markConfirmationAnswered(id)
      emit('answered', approved)
    }
  } catch {
    respondError.value = t('tools.mcp.approvalFailed')
  } finally {
    responding.value = false
  }
}
</script>
