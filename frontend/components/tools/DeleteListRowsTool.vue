<!--
  Chat card for delete_list_rows.

  - Finding: the agent is matching the rows to delete.
  - Confirm: the matching rows (key + a few values) with Delete / Cancel.
    Deletion is permanent, so the Delete button is red and says how many.
  - Deleting → Deleted: how many rows went and how many remain, plus a link
    to the list.
  - Cancelled / timed out / nothing matched / failed: a one-line header.
-->
<template>
  <div class="mt-1" data-testid="delete-list-rows-tool">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-chat-2 text-gray-500 dark:text-gray-400">
        <span v-if="phase === 'finding'" class="tool-shimmer flex items-center min-w-0">
          <Icon name="heroicons-trash" class="w-3 h-3 me-1 text-ink-muted shrink-0" />
          <span class="truncate" dir="auto">{{ $t('tools.deleteListRows.finding', { name: listName }) }}</span>
        </span>
        <span v-else-if="phase === 'confirm'" class="flex items-center text-gray-700 dark:text-gray-300">
          <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-red-500" />
          <span :class="readonly ? 'tool-shimmer' : ''">{{ readonly ? $t('tools.deleteListRows.waiting') : $t('tools.deleteListRows.confirm') }}</span>
        </span>
        <span v-else-if="phase === 'deleting'" class="flex items-center">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-ink-muted" />
          <span class="tool-shimmer">{{ $t('tools.deleteListRows.deleting') }}</span>
        </span>
        <span v-else-if="phase === 'done'" class="text-gray-700 dark:text-gray-300 flex items-center" data-testid="delete-list-rows-done">
          <Icon name="heroicons-trash" class="w-3 h-3 me-1 text-emerald-500" />
          <span dir="auto">{{ $t('tools.deleteListRows.deleted', { n: output.deleted, name: listName }, Number(output.deleted || 0)) }}</span>
        </span>
        <span v-else-if="phase === 'cancelled'" class="text-gray-700 dark:text-gray-300 flex items-center" data-testid="delete-list-rows-cancelled">
          <Icon name="heroicons-arrow-uturn-left" class="w-3 h-3 me-1 text-ink-muted" />
          <span>{{ outputStatus === 'timed_out' ? $t('tools.deleteListRows.timedOut') : $t('tools.deleteListRows.cancelled') }}</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-amber-500" />
          <span>{{ outputStatus === 'no_match' ? $t('tools.deleteListRows.noMatch') : $t('tools.deleteListRows.failed') }}</span>
        </span>
      </div>
    </Transition>

    <div v-if="phase === 'failed' && message" class="text-xs text-gray-500 dark:text-gray-400 ms-1 mb-2 max-w-chat" dir="auto">{{ message }}</div>

    <Transition name="fade" appear>
      <div
        v-if="expanded && previewRows.length"
        class="rounded-lg border bg-white dark:bg-gray-900 max-w-chat"
        :class="phase === 'confirm' ? 'border-red-200 dark:border-red-900/60' : 'border-gray-200 dark:border-gray-800'"
        data-testid="delete-list-rows-card"
      >
        <!-- Header -->
        <div class="px-3 pt-2.5 pb-2">
          <div class="flex items-center gap-2 min-w-0">
            <Icon name="heroicons-table-cells" class="w-4 h-4 text-red-500 flex-shrink-0" />
            <span class="text-sm font-medium text-gray-800 dark:text-gray-200 truncate" dir="auto">{{ listName }}</span>
            <span class="text-2xs px-1.5 py-0.5 rounded-full flex-shrink-0 bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-400" data-testid="delete-list-rows-count">
              {{ clearAll ? $t('tools.deleteListRows.badgeClear') : $t('tools.deleteListRows.badgeCount', { n: count, total }, count) }}
            </span>
            <span class="flex-1"></span>
            <span v-if="agentName" class="hidden sm:inline text-2xs text-ink-muted truncate" dir="auto">{{ agentName }}</span>
          </div>
          <div v-if="phase === 'confirm'" class="mt-1.5 text-2xs text-red-700 dark:text-red-400 flex items-start gap-1">
            <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 mt-0.5 flex-shrink-0" />
            <span>{{ $t('tools.deleteListRows.permanent') }}</span>
          </div>
        </div>

        <!-- Rows -->
        <div class="border-t border-gray-100 dark:border-gray-800 px-3 py-2 overflow-x-auto">
          <table class="w-full text-2xs" data-testid="delete-list-rows-preview">
            <thead>
              <tr class="text-2xs uppercase tracking-wide text-ink-muted">
                <th v-for="c in columns" :key="c.name" class="text-start font-normal pe-3 pb-1 whitespace-nowrap">
                  <span class="inline-flex items-center gap-1">
                    <Icon v-if="c.key" name="heroicons-key" class="w-2.5 h-2.5 text-amber-500" />
                    <span class="font-mono normal-case">{{ c.name }}</span>
                  </span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in previewRows" :key="r.row_id" class="text-gray-700 dark:text-gray-300" :class="phase === 'done' ? 'line-through opacity-60' : ''">
                <td v-for="c in columns" :key="c.name" class="pe-3 py-0.5 max-w-[14rem] truncate" dir="auto" :title="r.values?.[c.name] || ''">
                  {{ r.values?.[c.name] ?? '—' }}
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="count > previewRows.length" class="mt-1 text-2xs text-ink-muted">
            {{ $t('tools.deleteListRows.andMore', { n: count - previewRows.length }, count - previewRows.length) }}
          </div>
          <div v-if="notFound.length" class="mt-1 text-2xs text-ink-muted" dir="auto">
            {{ $t('tools.deleteListRows.notFound', { list: notFound.join(', ') }) }}
          </div>
        </div>

        <!-- Confirm actions -->
        <div v-if="interactive" class="border-t border-gray-100 dark:border-gray-800 px-3 py-2" data-testid="delete-list-rows-confirm">
          <div v-if="reason" class="mb-1.5 text-2xs text-gray-500 dark:text-gray-400 italic" dir="auto">{{ reason }}</div>
          <div class="flex items-center gap-2">
            <span class="flex-1"></span>
            <button
              class="px-2.5 py-1 text-xs rounded-md text-gray-500 hover:text-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50 inline-flex items-center gap-1"
              :disabled="responding"
              data-testid="delete-list-rows-cancel"
              @click="respond(false)"
            >
              <Spinner v-if="responding && pendingChoice === false" class="w-3 h-3" />
              {{ $t('tools.deleteListRows.cancel') }}
            </button>
            <button
              class="px-2.5 py-1 text-xs font-medium rounded-md text-white bg-red-600 hover:bg-red-700 disabled:opacity-50 inline-flex items-center gap-1"
              :disabled="responding"
              data-testid="delete-list-rows-approve"
              @click="respond(true)"
            >
              <Spinner v-if="responding && pendingChoice === true" class="w-3 h-3" />
              {{ $t('tools.deleteListRows.approve', { n: count }, count) }}
            </button>
          </div>
          <div v-if="respondError" class="mt-1 text-2xs text-red-500">{{ respondError }}</div>
        </div>

        <!-- Result footer -->
        <div v-if="phase === 'done'" class="border-t border-gray-100 dark:border-gray-800 px-3 py-1.5 flex items-center gap-3 text-2xs text-ink-muted" data-testid="delete-list-rows-result">
          <span>{{ $t('tools.deleteListRows.remaining', { n: output.remaining ?? 0 }, Number(output.remaining ?? 0)) }}</span>
          <span class="flex-1"></span>
          <NuxtLink
            v-if="!readonly && output.agent_id && output.list_id"
            :to="`/agents/${output.agent_id}/lists/${output.list_id}`"
            class="text-blue-600 hover:text-blue-800 dark:text-blue-400 inline-flex items-center gap-1 flex-shrink-0"
          >
            <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
            {{ $t('tools.listSchema.open') }}
          </NuxtLink>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'

const { t } = useI18n()

const props = defineProps<{
  toolExecution: {
    tool_name?: string
    status: string
    arguments_json?: any
    result_json?: any
    confirmation?: any
    progress_stage?: string
    review_state?: string
  }
  systemCompletionId?: string
  // Shared conversation (/c/<token>): show state, never the buttons or links.
  readonly?: boolean
}>()

const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const status = computed<string>(() => props.toolExecution?.status || '')
const isRunning = computed(() => ['running', 'in_progress'].includes(status.value))
const reviewState = computed<string>(() => props.toolExecution?.review_state || '')
const readonly = computed(() => !!props.readonly)
const result = computed<any>(() => props.toolExecution?.result_json || {})
const output = computed<any>(() => (result.value?.output && typeof result.value.output === 'object') ? result.value.output : result.value)
const outputStatus = computed<string>(() => output.value?.status || '')
const message = computed<string>(() => output.value?.message || '')
const confirmation = computed<any>(() => props.toolExecution?.confirmation || null)
const progressStage = computed<string>(() => props.toolExecution?.progress_stage || '')

const responded = ref(false)
const responding = ref(false)
const pendingChoice = ref<boolean | null>(null)
const respondError = ref('')

const phase = computed<'finding' | 'confirm' | 'deleting' | 'done' | 'cancelled' | 'failed'>(() => {
  if (isRunning.value) {
    if (!responded.value && confirmation.value?.confirmation_id &&
        ['awaiting_confirmation', 'awaiting_approval'].includes(progressStage.value)) return 'confirm'
    if (reviewState.value === 'pending' && !responded.value) return 'confirm'
    if (responded.value || reviewState.value === 'approved') return 'deleting'
    return 'finding'
  }
  if (output.value?.success) return 'done'
  if (['rejected', 'timed_out'].includes(outputStatus.value)) return 'cancelled'
  return 'failed'
})

const expanded = computed(() => ['confirm', 'deleting', 'done'].includes(phase.value))
const interactive = computed(() => phase.value === 'confirm' && !readonly.value && !!confirmation.value?.confirmation_id)

const listName = computed<string>(() => output.value?.list_name || confirmation.value?.list_name || args.value?.list_id || '')
const agentName = computed<string>(() => output.value?.agent_name || confirmation.value?.agent_name || '')
const columns = computed<{ name: string; key?: boolean }[]>(() => confirmation.value?.columns || output.value?.columns || [])
const previewRows = computed<any[]>(() => confirmation.value?.rows || output.value?.preview || [])
const count = computed<number>(() => Number(confirmation.value?.count ?? output.value?.matched ?? 0))
const total = computed<number>(() => Number(confirmation.value?.total ?? output.value?.total ?? 0))
const clearAll = computed<boolean>(() => !!(confirmation.value?.clear_all ?? args.value?.all))
const notFound = computed<string[]>(() => confirmation.value?.not_found || output.value?.not_found || [])
const reason = computed<string>(() => confirmation.value?.reason || args.value?.reason || '')

const { markConfirmationAnswered } = useToolConfirmations()

async function respond(approved: boolean) {
  const cid = confirmation.value?.confirmation_id
  if (!cid || !props.systemCompletionId || responding.value) return
  responding.value = true
  pendingChoice.value = approved
  respondError.value = ''
  try {
    const { error } = await useMyFetch(`/completions/${props.systemCompletionId}/mcp_tool_confirmations/${cid}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved, remember: false, response: {} }),
    })
    if (error?.value) {
      const code = (error.value as any)?.statusCode
      respondError.value = code === 410 ? t('tools.listSchema.expired') : t('tools.listSchema.respondFailed')
      return
    }
    responded.value = true
    markConfirmationAnswered(cid)
  } catch (e) {
    respondError.value = t('tools.listSchema.respondFailed')
  } finally {
    responding.value = false
    pendingChoice.value = null
  }
}
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer { 0% { background-position: 0% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
