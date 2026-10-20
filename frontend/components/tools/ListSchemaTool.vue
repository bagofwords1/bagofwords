<!--
  Chat card for create_list / edit_list (training mode).

  - Proposing: the agent is still writing the schema.
  - Review: the proposed schema (fields, key, source-quote rule) with Approve /
    Request changes. For an edit, each field is marked new / changed / removed
    and the header says whether the change is additive or BREAKING.
  - Saving → Created / Updated: the final schema, the submit_<slug> tool and
    the bow.<agent>.lists.<list> table, plus a link to the list.
  - Declined / timed out: collapsed header with the user's feedback.
  - Failed: the tool's message and the schema errors the agent was asked to fix.
-->
<template>
  <div class="mt-1" data-testid="list-schema-tool">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="phase === 'proposing'" class="tool-shimmer flex items-center min-w-0">
          <Icon name="heroicons-list-bullet" class="w-3 h-3 me-1 text-gray-400 shrink-0" />
          <span class="truncate" dir="auto">{{ isEdit ? $t('tools.listSchema.proposingEdit', { name: listName }) : $t('tools.listSchema.proposing', { name: listName }) }}</span>
        </span>
        <span v-else-if="phase === 'review'" class="flex items-center text-gray-700 dark:text-gray-300">
          <Icon name="heroicons-clipboard-document-check" class="w-3 h-3 me-1 text-blue-500" />
          <span :class="readonly ? 'tool-shimmer' : ''">{{ readonly ? $t('tools.listSchema.waitingReview') : (isEdit ? $t('tools.listSchema.reviewEdit') : $t('tools.listSchema.review')) }}</span>
        </span>
        <span v-else-if="phase === 'saving'" class="flex items-center">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
          <span class="tool-shimmer">{{ $t('tools.listSchema.saving') }}</span>
        </span>
        <span v-else-if="phase === 'done'" class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-list-bullet" class="w-3 h-3 me-1 text-emerald-500" />
          <span>{{ isEdit ? $t('tools.listSchema.updated') : $t('tools.listSchema.created') }}</span>
        </span>
        <span v-else-if="phase === 'declined'" class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-arrow-uturn-left" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ outputStatus === 'timed_out' ? $t('tools.listSchema.timedOut') : $t('tools.listSchema.declined') }}</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-amber-500" />
          <span>{{ outputStatus === 'no_change' ? $t('tools.listSchema.noChange') : $t('tools.listSchema.failed') }}</span>
        </span>
      </div>
    </Transition>

    <div v-if="phase === 'failed' && message" class="text-xs text-gray-500 dark:text-gray-400 ms-1 mb-2 max-w-2xl" dir="auto">
      {{ message }}
      <ul v-if="errors.length" class="mt-1 list-disc ms-4 space-y-0.5" data-testid="list-schema-errors">
        <li v-for="e in errors" :key="e" class="font-mono text-[11px]">{{ e }}</li>
      </ul>
    </div>
    <div v-if="phase === 'declined' && feedback" class="text-xs text-gray-500 dark:text-gray-400 ms-1 mb-2 italic" dir="auto">
      “{{ feedback }}”
    </div>

    <Transition name="fade" appear>
      <div
        v-if="fields.length && expanded"
        class="rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 max-w-2xl"
        data-testid="list-schema-card"
      >
        <!-- Header -->
        <div class="px-3 pt-2.5 pb-2">
          <div class="flex items-center gap-2 min-w-0">
            <Icon name="heroicons-table-cells" class="w-4 h-4 text-blue-500 flex-shrink-0" />
            <span class="text-sm font-medium text-gray-800 dark:text-gray-200 truncate" dir="auto" data-testid="list-schema-name">{{ listName }}</span>
            <span v-if="previousName" class="text-[11px] text-gray-400 line-through truncate" dir="auto">{{ previousName }}</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full flex-shrink-0" :class="badgeClass" data-testid="list-schema-badge">{{ badgeLabel }}</span>
            <span class="flex-1"></span>
            <span v-if="agentName" class="hidden sm:inline text-[10px] text-gray-400 truncate" dir="auto">{{ agentName }}</span>
          </div>
          <div v-if="description" class="mt-1 text-[11px] text-gray-500 dark:text-gray-400 leading-snug" dir="auto">{{ description }}</div>
          <div v-if="isEdit && change === 'breaking' && phase !== 'done'" class="mt-1.5 text-[11px] text-amber-700 dark:text-amber-400 flex items-start gap-1" data-testid="list-schema-breaking">
            <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 mt-0.5 flex-shrink-0" />
            <span>{{ $t('tools.listSchema.breakingHint', { n: rowCount, v: (version || 1) + 1 }, rowCount) }}</span>
          </div>
        </div>

        <!-- Fields -->
        <div class="border-t border-gray-100 dark:border-gray-800 px-3 py-2">
          <div class="flex items-center text-[10px] uppercase tracking-wide text-gray-400 mb-1">
            <span>{{ $t('tools.listSchema.fields', { count: visibleFields.length }) }}</span>
            <span class="flex-1"></span>
            <span v-if="keyField" class="normal-case tracking-normal inline-flex items-center gap-1" data-testid="list-schema-key">
              <Icon name="heroicons-key" class="w-2.5 h-2.5 text-amber-500" />
              <span class="font-mono">{{ keyField }}</span>
            </span>
          </div>
          <ul class="space-y-0.5" data-testid="list-schema-fields">
            <li
              v-for="f in fields" :key="f.name"
              class="text-[11px] flex items-start gap-2 py-0.5 rounded px-1 -mx-1"
              :class="rowClass(f)"
              :data-testid="`list-field-${f.name}`"
            >
              <span class="w-3.5 h-3.5 flex items-center justify-center flex-shrink-0 mt-px">
                <Icon v-if="f.key" name="heroicons-key" class="w-3 h-3 text-amber-500" />
                <span v-else class="w-1 h-1 rounded-full" :class="f.required ? 'bg-gray-500 dark:bg-gray-400' : 'bg-gray-200 dark:bg-gray-700'" :title="f.required ? $t('tools.listSchema.required') : ''"></span>
              </span>
              <span class="font-mono text-gray-700 dark:text-gray-300 truncate flex-shrink-0 max-w-[40%]" :class="f.status === 'removed' ? 'line-through text-gray-400' : ''">{{ f.name }}</span>
              <span class="text-gray-400 flex-shrink-0">{{ typeLabel(f.type) }}<template v-if="f.unit"> · {{ f.unit }}</template></span>
              <span v-if="f.enum && f.enum.length" class="text-gray-400 truncate hidden sm:inline" :title="f.enum.join(', ')">{{ f.enum.join(' / ') }}</span>
              <span v-else-if="f.description" class="text-gray-400 truncate hidden sm:inline" dir="auto">{{ f.description }}</span>
              <span class="flex-1"></span>
              <span v-if="f.status && f.status !== 'unchanged'" class="text-[9px] px-1 py-0.5 rounded flex-shrink-0" :class="statusClass(f.status)">{{ $t('tools.listSchema.status.' + f.status) }}</span>
            </li>
          </ul>
          <div v-if="requireEvidence || keepHumanEdits" class="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-gray-400">
            <span v-if="requireEvidence" class="inline-flex items-center gap-1"><Icon name="heroicons-document-magnifying-glass" class="w-3 h-3" />{{ $t('tools.listSchema.requireEvidence') }}</span>
            <span v-if="keepHumanEdits" class="inline-flex items-center gap-1"><Icon name="heroicons-lock-closed" class="w-3 h-3" />{{ $t('tools.listSchema.keepHumanEdits') }}</span>
          </div>
        </div>

        <!-- Review actions -->
        <div v-if="interactive" class="border-t border-gray-100 dark:border-gray-800 px-3 py-2" data-testid="list-schema-review">
          <div v-if="reason" class="mb-1.5 text-[11px] text-gray-500 dark:text-gray-400 italic" dir="auto">{{ reason }}</div>
          <div v-if="showFeedback" class="mb-2">
            <textarea
              v-model="feedbackDraft"
              rows="2"
              :placeholder="$t('tools.listSchema.feedbackPlaceholder')"
              class="w-full text-xs rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400 resize-none"
              data-testid="list-schema-feedback"
            ></textarea>
          </div>
          <div class="flex items-center gap-2">
            <span class="flex-1"></span>
            <template v-if="!showFeedback">
              <button
                class="px-2.5 py-1 text-xs rounded-md text-gray-500 hover:text-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                :disabled="responding"
                data-testid="list-schema-request-changes"
                @click="showFeedback = true"
              >{{ $t('tools.listSchema.requestChanges') }}</button>
              <button
                class="px-2.5 py-1 text-xs font-medium rounded-md text-white disabled:opacity-50 inline-flex items-center gap-1"
                :class="change === 'breaking' ? 'bg-amber-600 hover:bg-amber-700' : 'bg-blue-600 hover:bg-blue-700'"
                :disabled="responding"
                data-testid="list-schema-approve"
                @click="respond(true)"
              >
                <Spinner v-if="responding && pendingChoice === true" class="w-3 h-3" />
                {{ isEdit ? $t('tools.listSchema.approveEdit') : $t('tools.listSchema.approveCreate') }}
              </button>
            </template>
            <template v-else>
              <button
                class="px-2.5 py-1 text-xs rounded-md text-gray-500 hover:text-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800"
                :disabled="responding"
                @click="showFeedback = false"
              >{{ $t('tools.listSchema.cancel') }}</button>
              <button
                class="px-2.5 py-1 text-xs font-medium rounded-md bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 hover:opacity-90 disabled:opacity-50 inline-flex items-center gap-1"
                :disabled="responding"
                data-testid="list-schema-send-feedback"
                @click="respond(false)"
              >
                <Spinner v-if="responding && pendingChoice === false" class="w-3 h-3" />
                {{ $t('tools.listSchema.sendFeedback') }}
              </button>
            </template>
          </div>
          <div v-if="respondError" class="mt-1 text-[10px] text-red-500">{{ respondError }}</div>
        </div>

        <!-- Result footer -->
        <div v-if="phase === 'done'" class="border-t border-gray-100 dark:border-gray-800 px-3 py-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-gray-400" data-testid="list-schema-result">
          <span v-if="output.tool_name" class="inline-flex items-center gap-1 min-w-0"><Icon name="heroicons-wrench-screwdriver" class="w-3 h-3 flex-shrink-0" /><span class="truncate">{{ $t('tools.listSchema.toolHint') }} <bdi class="font-mono">{{ output.tool_name }}</bdi></span></span>
          <span v-if="output.table_name" class="inline-flex items-center gap-1 min-w-0"><Icon name="heroicons-circle-stack" class="w-3 h-3 flex-shrink-0" /><span class="truncate">{{ $t('tools.listSchema.queryHint') }} <bdi class="font-mono">{{ output.table_name }}</bdi></span></span>
          <span v-if="isEdit && output.change === 'breaking'" class="text-amber-600 dark:text-amber-400">{{ $t('tools.listSchema.versionBumped', { v: output.version }) }}</span>
          <span class="flex-1"></span>
          <NuxtLink
            v-if="!readonly && output.agent_id && output.list_id"
            :to="`/agents/${output.agent_id}/lists/${output.list_id}`"
            class="text-blue-600 hover:text-blue-800 dark:text-blue-400 inline-flex items-center gap-1 flex-shrink-0"
            data-testid="list-schema-open"
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
  // Shared conversation (/c/<token>): show state, never review controls or
  // links into the workspace.
  readonly?: boolean
}>()

type Field = { name: string; type: string; required?: boolean; key?: boolean; enum?: string[] | null; unit?: string | null; description?: string; status?: string }

const isEdit = computed(() => (props.toolExecution?.tool_name || '') === 'edit_list')
const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const status = computed<string>(() => props.toolExecution?.status || '')
const isRunning = computed(() => ['running', 'in_progress'].includes(status.value))
const reviewState = computed<string>(() => props.toolExecution?.review_state || '')
const readonly = computed(() => !!props.readonly)
const result = computed<any>(() => props.toolExecution?.result_json || {})
const output = computed<any>(() => (result.value?.output && typeof result.value.output === 'object') ? result.value.output : result.value)
const outputStatus = computed<string>(() => output.value?.status || '')
const message = computed<string>(() => output.value?.message || '')
const errors = computed<string[]>(() => Array.isArray(output.value?.errors) ? output.value.errors : [])
const feedback = computed<string>(() => output.value?.feedback || '')
const confirmation = computed<any>(() => props.toolExecution?.confirmation || null)
const progressStage = computed<string>(() => props.toolExecution?.progress_stage || '')

const responded = ref(false)
const responding = ref(false)
const pendingChoice = ref<boolean | null>(null)
const respondError = ref('')
const showFeedback = ref(false)
const feedbackDraft = ref('')

const phase = computed<'proposing' | 'review' | 'saving' | 'done' | 'declined' | 'failed'>(() => {
  if (isRunning.value) {
    if (!responded.value && confirmation.value?.confirmation_id &&
        ['awaiting_confirmation', 'awaiting_approval'].includes(progressStage.value)) return 'review'
    if (reviewState.value === 'pending' && !responded.value) return 'review'
    if (responded.value || reviewState.value === 'approved') return 'saving'
    return 'proposing'
  }
  if (output.value?.success) return 'done'
  if (['rejected', 'timed_out'].includes(outputStatus.value)) return 'declined'
  return 'failed'
})

// A declined or failed proposal collapses to its header so the revised one
// that follows stands out.
const expanded = computed(() => !['declined', 'failed'].includes(phase.value))
const interactive = computed(() => phase.value === 'review' && !readonly.value && !!confirmation.value?.confirmation_id)

// Schema source, by phase: the saved list → the reviewed proposal → the call's arguments.
const fields = computed<Field[]>(() => {
  if (phase.value === 'done' && Array.isArray(output.value?.fields)) return output.value.fields
  if (Array.isArray(confirmation.value?.fields)) return confirmation.value.fields
  if (isEdit.value) {
    return [
      ...((args.value?.add_fields || []) as any[]).map(f => ({ ...f, status: 'added' })),
      ...((args.value?.update_fields || []) as any[]).map(f => ({ ...f, name: f.new_name || f.name, type: f.type || 'string', status: 'changed' })),
      ...((args.value?.remove_fields || []) as string[]).map(n => ({ name: n, type: 'string', status: 'removed' })),
    ]
  }
  const key = args.value?.key_field
  return ((args.value?.fields || []) as any[]).map(f => ({ ...f, key: !!key && f.name === key }))
})
const visibleFields = computed(() => fields.value.filter(f => f.status !== 'removed'))
const keyField = computed<string>(() => output.value?.key_field || confirmation.value?.key_field || fields.value.find(f => f.key)?.name || (isEdit.value ? '' : (args.value?.key_field || '')))
const listName = computed<string>(() => output.value?.list_name || confirmation.value?.list_name || args.value?.name || args.value?.list_id || '')
const previousName = computed<string>(() => confirmation.value?.previous_name || '')
const agentName = computed<string>(() => output.value?.agent_name || confirmation.value?.agent_name || '')
const description = computed<string>(() => confirmation.value?.description ?? (isEdit.value ? (args.value?.description || '') : (args.value?.description || '')))
const reason = computed<string>(() => confirmation.value?.reason || args.value?.reason || '')
const change = computed<string>(() => output.value?.change || confirmation.value?.change || '')
const version = computed<number>(() => Number(confirmation.value?.version || output.value?.version || 1))
const rowCount = computed<number>(() => Number(confirmation.value?.row_count || output.value?.row_count || 0))
const requireEvidence = computed<boolean>(() => confirmation.value?.require_evidence ?? args.value?.require_evidence ?? false)
const keepHumanEdits = computed<boolean>(() => confirmation.value?.keep_human_edits ?? args.value?.keep_human_edits ?? false)

const badgeLabel = computed(() => {
  if (!isEdit.value) return t('tools.listSchema.badgeCreate')
  if (change.value === 'breaking') return t('tools.listSchema.badgeBreaking')
  return t('tools.listSchema.badgeEdit')
})
const badgeClass = computed(() => isEdit.value && change.value === 'breaking'
  ? 'bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400'
  : 'bg-gray-100 dark:bg-gray-800 text-gray-500')

function typeLabel(type: string): string {
  const key = `lists.editor.types.${type || 'string'}`
  const label = t(key)
  return label === key ? (type || 'string') : label
}
function rowClass(f: Field): string {
  if (f.status === 'added') return 'bg-emerald-50/60 dark:bg-emerald-950/20'
  if (f.status === 'changed') return 'bg-amber-50/60 dark:bg-amber-950/20'
  if (f.status === 'removed') return 'opacity-60'
  return ''
}
function statusClass(s: string): string {
  if (s === 'added') return 'bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-400'
  if (s === 'changed') return 'bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-400'
  if (s === 'removed') return 'bg-gray-100 dark:bg-gray-800 text-gray-500'
  return ''
}

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
      body: JSON.stringify({
        approved,
        remember: false,
        response: approved ? {} : { feedback: feedbackDraft.value.trim() },
      }),
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
