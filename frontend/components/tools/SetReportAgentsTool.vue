<template>
  <div class="mt-1 text-xs">
    <div class="flex items-center text-gray-700 dark:text-gray-300">
      <Icon name="heroicons-view-columns" class="w-3 h-3 me-1 text-gray-400 flex-shrink-0" />
      <span v-if="showApprovalCard" class="tool-shimmer">Waiting for your approval…</span>
      <span v-else-if="status === 'running'" class="tool-shimmer">Setting agent focus…</span>
      <!-- The status row talks about AGENTS (the report's context), so it
           carries the neutral agent cube — the connector logo stays on the
           approval card below, where identifying the source matters. -->
      <span v-else-if="names.length" class="flex items-center min-w-0">
        <span class="flex-shrink-0">{{ addedNames.length ? 'Added' : 'Focused on' }}</span>
        <span v-for="n in names" :key="n" class="ms-1.5 flex items-center gap-1 min-w-0">
          <Icon name="heroicons-cube" class="w-3.5 h-3.5 flex-shrink-0 text-gray-400" />
          <span class="font-medium truncate">{{ n }}</span>
        </span>
        <span v-if="addedNames.length" class="ms-1 flex-shrink-0">to the report</span>
      </span>
      <span v-else-if="approval && !approval.approved">
        {{ approval.timed_out ? 'Agent request timed out' : 'Agent request declined' }}
      </span>
      <span v-else>Cleared agent focus — back to Auto</span>
    </div>

    <!-- Approval: one quiet line, not a warning banner -->
    <div
      v-if="showApprovalCard"
      class="mt-2 max-w-md border border-gray-200 dark:border-gray-800 rounded-lg px-3 py-2 bg-white dark:bg-gray-900"
      data-testid="agent-approval-card"
    >
      <div class="flex items-center gap-3">
        <span class="text-xs text-gray-800 dark:text-gray-200 min-w-0 truncate flex items-center gap-1">
          <span>Add</span>
          <template v-for="a in confAgents" :key="a.id">
            <DataSourceIcon :type="a.type" :connector-key="a.connector_key" :icon="a.icon" class="w-3.5 h-3.5 flex-shrink-0" />
            <span class="font-medium">{{ a.name }}</span>
          </template>
          <span v-if="!confAgents.length" class="font-medium">{{ confAgentNames.join(', ') }}</span>
          <span>to this report?</span>
        </span>
        <span class="ms-auto flex items-center gap-1 flex-shrink-0">
          <button
            @click="respond(true)" :disabled="responding"
            class="px-2.5 py-1 text-xs font-medium rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          >Allow</button>
          <button
            @click="respond(false)" :disabled="responding"
            class="px-2.5 py-1 text-xs rounded-md text-gray-500 hover:text-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
          >Deny</button>
        </span>
      </div>
      <div v-if="confReason" class="mt-0.5 text-[11px] text-gray-400 dark:text-gray-500 truncate">{{ confReason }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

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
  status.value === 'running' &&
  ['awaiting_confirmation', 'awaiting_approval'].includes(props.toolExecution?.progress_stage || ''))

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
  } catch (e) {
    console.error('agent approval respond failed', e)
  } finally {
    responding.value = false
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
</style>
