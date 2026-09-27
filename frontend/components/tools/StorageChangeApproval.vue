<template>
  <div
    v-if="active || decision !== null || respondError !== null"
    class="mt-2 ms-[18px] max-w-md rounded-md border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950 p-2.5 space-y-2"
    role="group"
    :aria-label="t('tools.storageChange.title')"
    data-testid="storage-change-approval"
  >
    <div class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ t('tools.storageChange.title') }}</div>
    <ul class="space-y-1.5">
      <li v-for="(item, idx) in items" :key="idx" class="text-xs text-gray-700 dark:text-gray-300">
        <div class="break-words">{{ t(item.key, item.params) }}</div>
        <div class="text-[11px] text-gray-500 dark:text-gray-400">{{ t('tools.storageChange.impact', item.impact) }}</div>
      </li>
    </ul>
    <div v-if="decision === null && respondError !== 'expired'" class="flex items-center gap-2">
      <button
        type="button"
        class="px-2.5 py-1 text-xs font-medium text-white bg-blue-600 rounded hover:bg-blue-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
        :disabled="responding || !canRespond"
        @click="respond(true)"
      >{{ t('tools.storageChange.allow') }}</button>
      <button
        type="button"
        class="px-2.5 py-1 text-xs font-medium text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded hover:bg-gray-50 dark:hover:bg-gray-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-50"
        :disabled="responding || !canRespond"
        @click="respond(false)"
      >{{ t('tools.storageChange.deny') }}</button>
    </div>
    <div v-else-if="decision !== null" class="text-[11px] text-gray-500 dark:text-gray-400" role="status">
      {{ decision ? t('tools.storageChange.approved') : t('tools.storageChange.declined') }}
    </div>
    <div v-if="respondError" class="text-[11px] text-red-600 dark:text-red-400" role="alert">
      {{ respondError === 'expired' ? t('tools.storageChange.expired') : t('tools.storageChange.failed') }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { approvalOutcome, storageChangeItems } from '~/utils/toolConfirmation'

// Durable builtin confirmation for destructive storage-declaration changes
// (create_artifact rebuild / edit_artifact). Answered only through the
// authenticated completion route, which checks that the run's user answers.
const props = defineProps<{
  confirmation: any
  systemCompletionId?: string
  active: boolean
}>()

const { t } = useI18n()

const items = computed(() => storageChangeItems(props.confirmation))
const canRespond = computed(() => !!props.confirmation?.confirmation_id && !!props.systemCompletionId)
const responding = ref(false)
const decision = ref<boolean | null>(null)
// Shown inline: a silent failure makes the buttons look dead while the run
// waits out its approval timeout. 'failed' keeps the buttons for a retry;
// 'expired' (410) means the wait is over, so they are hidden.
const respondError = ref<'failed' | 'expired' | null>(null)

async function respond(approved: boolean) {
  const cid = props.confirmation?.confirmation_id
  if (!cid || !props.systemCompletionId || responding.value || decision.value !== null) return
  responding.value = true
  respondError.value = null
  try {
    const res = await useMyFetch(`/completions/${props.systemCompletionId}/mcp_tool_confirmations/${cid}`, {
      method: 'POST',
      body: { approved, remember: false },
    })
    const outcome = approvalOutcome(res, approved)
    if (outcome.error) {
      console.warn('storage change approval failed', (res as any)?.error?.value)
      respondError.value = outcome.error
      return
    }
    decision.value = outcome.decision
  } catch (e) {
    console.warn('storage change approval failed', e)
    respondError.value = approvalOutcome({ error: { value: e } }, approved).error
  } finally {
    responding.value = false
  }
}
</script>
