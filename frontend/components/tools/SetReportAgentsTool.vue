<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-user-group" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="showApprovalCard">Waiting for your approval…</template>
      <template v-else-if="isRunning">Setting agent focus…</template>
      <template v-else-if="names.length">{{ addedNames.length ? 'Added' : 'Focused on' }}</template>
      <template v-else-if="approval && !approval.approved">{{ approval.timed_out ? 'Agent request timed out' : 'Agent request declined' }}</template>
      <template v-else>Cleared agent focus — back to Auto</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded || (showApprovalCard)" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="showApprovalCard"></template>
      <template v-else-if="isRunning"></template>
      <template v-else-if="names.length"><span v-for="n in names" :key="n" class="ms-1.5 flex items-center gap-1 min-w-0"><Icon name="heroicons-cube" class="w-3.5 h-3.5 flex-shrink-0 text-ink-muted" /><span class="font-medium truncate">{{ n }}</span></span><span v-if="addedNames.length" class="ms-1 flex-shrink-0">to the report</span></template>
      <template v-else-if="approval && !approval.approved"></template>
      <template v-else></template></div>

    <!-- Approval: one quiet line, not a warning banner -->
    <div
      v-if="showApprovalCard"
      class="mt-2 max-w-md border border-gray-200 dark:border-gray-800 rounded-lg px-3 py-2 bg-white dark:bg-gray-900"
      data-testid="agent-approval-card"
    >
      <div class="flex items-center gap-3">
        <span class="text-chat-2 text-gray-800 dark:text-gray-200 min-w-0 truncate flex items-center gap-1">
          <span>Add</span>
          <template v-for="a in confAgents" :key="a.id">
            <DataSourceIcon :type="a.type" :connector-key="a.connector_key" :icon-token="a.icon_token" :icon="a.icon" class="w-3.5 h-3.5 flex-shrink-0" />
            <span class="font-medium">{{ a.name }}</span>
          </template>
          <span v-if="!confAgents.length" class="font-medium">{{ confAgentNames.join(', ') }}</span>
          <span>to this report?</span>
        </span>
        <span class="ms-auto flex items-center gap-1 flex-shrink-0">
          <button
            @click="respond(true)" :disabled="responding"
            class="px-2.5 py-1 text-chat-2 font-medium rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          >Allow</button>
          <button
            @click="respond(false)" :disabled="responding"
            class="px-2.5 py-1 text-chat-2 rounded-md text-gray-500 hover:text-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
          >Deny</button>
        </span>
      </div>
      <div v-if="confReason" class="mt-0.5 text-chat-2 text-ink-muted truncate">{{ confReason }}</div>
    </div>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed, ref, watch } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { isToolRunning } from '~/utils/toolOutcome'

const props = defineProps<{
  toolExecution: {
    status: string
    result_json?: any
    confirmation?: any
    progress_stage?: string
  }
  systemCompletionId?: string
  dataSources?: any[]
}>()

const status = computed<string>(() => props.toolExecution?.status || '')
// Live stream says 'running'; a row loaded after reload says 'in_progress'.
const isRunning = computed(() => isToolRunning(status.value))
const names = computed<string[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.focused_agent_names) ? rj.focused_agent_names : []
})
const approval = computed<any>(() => (props.toolExecution?.result_json || {}).approval || null)
const addedNames = computed<string[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.added_agent_names) ? rj.added_agent_names : []
})

const confirmation = computed<any>(() => props.toolExecution?.confirmation || null)
const confAgentNames = computed<string[]>(() =>
  Array.isArray(confirmation.value?.agent_names) ? confirmation.value.agent_names : [])
const confAgents = computed<any[]>(() =>
  Array.isArray(confirmation.value?.agents) ? confirmation.value.agents : [])
const confReason = computed<string>(() => confirmation.value?.reason || '')

const responded = ref(false)
const responding = ref(false)

// The attach commits server-side when result_json lands (names appear) —
// nudge the page to refetch so DataSourceSelector reflects the new agent in
// real time instead of on the next page load.
const notified = ref(false)
watch(names, (v) => {
  if (v.length && !notified.value) {
    notified.value = true
    try { window.dispatchEvent(new CustomEvent('report:mutated', { detail: { kind: 'data_sources' } })) } catch {}
  }
})

const showApprovalCard = computed(() =>
  !!confirmation.value?.confirmation_id &&
  !responded.value &&
  isRunning.value &&
  ['awaiting_confirmation', 'awaiting_approval'].includes(props.toolExecution?.progress_stage || ''))

const { markConfirmationAnswered } = useToolConfirmations()

async function respond(approvedChoice: boolean) {
  const cid = confirmation.value?.confirmation_id
  if (!cid || !props.systemCompletionId || responding.value) return
  responding.value = true
  try {
    await useMyFetch(`/completions/${props.systemCompletionId}/mcp_tool_confirmations/${cid}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved: approvedChoice, remember: false }),
    })
    responded.value = true
    markConfirmationAnswered(cid)
  } catch (e) {
    console.error('agent approval respond failed', e)
  } finally {
    responding.value = false
  }
}
</script>

<style scoped>
.tool-call-details { overflow-wrap: anywhere; }
</style>
