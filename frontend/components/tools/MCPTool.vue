<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="flex items-center gap-1">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
          <span class="tool-shimmer">{{ runningLabel }}</span>
        </span>
        <span v-else class="text-gray-600 dark:text-gray-400 flex items-center gap-1">
          <DataSourceIcon v-if="connectorKey" type="mcp" :connector-key="connectorKey" class="w-3 h-3 me-1 shrink-0" />
          <McpIcon v-else-if="isExecuteMcp" class="w-3 h-3 me-1 shrink-0" />
          <Icon v-else name="heroicons-server-stack" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ doneLabel }}</span>
          <span v-if="duration" class="text-gray-400 ms-1">{{ duration }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 rtl-flip"
          />
        </span>
      </div>
    </Transition>

    <!-- Approval prompt ('ask' policy): the run is paused on this call.
         Deliberately quiet — an indented block under the status line, a
         one-line argument echo, and a row of small actions. -->
    <div
      v-if="showApprovalCard"
      class="mt-1.5 ms-1 border-s-2 border-amber-300/70 dark:border-amber-600/50 ps-2.5 py-0.5 space-y-1.5"
      data-testid="mcp-approval-card"
    >
      <button
        v-if="argsOneLine"
        type="button"
        class="block max-w-full truncate text-start text-[11px] font-mono text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
        :title="showArgs ? '' : argsOneLine"
        @click="showArgs = !showArgs"
      >{{ argsOneLine }}</button>
      <pre
        v-if="showArgs"
        class="max-h-32 overflow-auto rounded bg-gray-50 dark:bg-gray-900 text-[10px] leading-tight text-gray-600 dark:text-gray-400 p-2 m-0 whitespace-pre-wrap break-words font-mono"
      >{{ confirmationArgs }}</pre>
      <div class="flex flex-wrap items-center gap-1.5 text-xs">
        <button
          class="px-2 py-0.5 rounded font-medium text-white bg-blue-600 hover:bg-blue-700 transition-colors disabled:opacity-50"
          :disabled="responding" @click="respond(true, false)"
        >{{ $t('tools.mcp.allowOnce') }}</button>
        <button
          class="px-2 py-0.5 rounded text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-gray-700 dark:hover:text-gray-300 transition-colors disabled:opacity-50"
          :disabled="responding" :title="$t('tools.mcp.approvalHint')" @click="respond(true, true)"
        >{{ $t('tools.mcp.alwaysAllow') }}</button>
        <span class="h-3 w-px bg-gray-200 dark:bg-gray-700"></span>
        <button
          class="px-2 py-0.5 rounded text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-gray-700 dark:hover:text-gray-300 transition-colors disabled:opacity-50"
          :disabled="responding" @click="respond(false, false)"
        >{{ $t('tools.mcp.denyOnce') }}</button>
        <button
          class="px-2 py-0.5 rounded text-gray-400 dark:text-gray-500 hover:bg-red-50 dark:hover:bg-red-950 hover:text-red-600 dark:hover:text-red-400 transition-colors disabled:opacity-50"
          :disabled="responding" :title="$t('tools.mcp.approvalHint')" @click="respond(false, true)"
        >{{ $t('tools.mcp.alwaysDeny') }}</button>
      </div>
    </div>

    <!-- Auto policy verdict ('auto' policy): small-model review outcome -->
    <div
      v-if="autoPolicy"
      class="mt-1 flex items-center gap-1 text-[10px]"
      :class="autoPolicy.approved ? 'text-gray-400 dark:text-gray-500' : 'text-amber-600 dark:text-amber-500'"
      data-testid="mcp-auto-policy"
    >
      <Icon :name="autoPolicy.approved ? 'heroicons-shield-check' : 'heroicons-shield-exclamation'" class="w-3 h-3 shrink-0" />
      <span class="truncate">{{ autoPolicy.approved ? $t('tools.mcp.autoApproved') : $t('tools.mcp.autoDenied') }}<template v-if="autoPolicy.reason"> — {{ autoPolicy.reason }}</template></span>
    </div>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded && status !== 'running'" class="mt-2 space-y-1.5">
        <!-- Command (input) -->
        <div v-if="command" class="group">
          <div
            class="flex items-center text-[11px] text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-600 dark:hover:text-gray-300 mb-0.5"
            @click="showCommand = !showCommand"
          >
            <Icon
              :name="showCommand ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
              class="w-2.5 h-2.5 me-1 text-gray-400 rtl-flip"
            />
            <span>{{ $t('tools.common.input') }}</span>
          </div>
          <div v-if="showCommand" class="max-h-28 overflow-auto rounded bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800">
            <pre class="text-[10px] leading-tight text-gray-600 dark:text-gray-400 p-2 m-0 whitespace-pre-wrap break-words font-mono">{{ command }}</pre>
          </div>
        </div>

        <!-- Result preview -->
        <div v-if="preview" class="group">
          <div
            class="flex items-center text-[11px] text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-600 dark:hover:text-gray-300 mb-0.5"
            @click="showPreview = !showPreview"
          >
            <Icon
              :name="showPreview ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
              class="w-2.5 h-2.5 me-1 text-gray-400 rtl-flip"
            />
            <span>{{ $t('tools.common.output') }}</span>
          </div>
          <div v-if="showPreview" class="max-h-28 overflow-auto rounded bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800">
            <pre class="text-[10px] leading-tight text-gray-600 dark:text-gray-400 p-2 m-0 whitespace-pre-wrap break-words font-mono">{{ preview }}</pre>
          </div>
        </div>

        <!-- Error message -->
        <div v-if="errorMessage" class="text-[10px] text-red-500 bg-red-50/50 dark:bg-red-950 rounded px-2 py-1">
          {{ errorMessage }}
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import McpIcon from '~/components/icons/McpIcon.vue'
import Spinner from '~/components/Spinner.vue'
const { t } = useI18n()

interface ToolExecution {
  id: string
  tool_name: string
  tool_action?: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
  duration_ms?: number
}

const props = defineProps<{
  toolExecution: ToolExecution
  dataSources?: any[]
  systemCompletionId?: string | null
}>()

const isExpanded = ref(false)
const showPreview = ref(true)
const showCommand = ref(true)

const status = computed(() => props.toolExecution?.status || '')

// ── 'ask' policy: mid-run approval card ─────────────────────────────
const confirmation = computed(() => (props.toolExecution as any).confirmation || null)
const progressStage = computed(() => (props.toolExecution as any).progress_stage || '')
const answered = ref(false)
const responding = ref(false)

const showApprovalCard = computed(() =>
  !!confirmation.value?.confirmation_id &&
  !answered.value &&
  status.value === 'running' &&
  ['awaiting_confirmation', 'awaiting_approval'].includes(progressStage.value)
)

const confirmationArgs = computed(() => {
  const a = confirmation.value?.arguments
  if (!a || !Object.keys(a).length) return ''
  try { return JSON.stringify(a, null, 2) } catch { return String(a) }
})

// Compact one-line call echo, e.g. create_item({"board_id": 1, …}); click to expand.
const showArgs = ref(false)
const argsOneLine = computed(() => {
  if (!confirmation.value) return ''
  const name = confirmation.value.tool_name || 'tool'
  const a = confirmation.value.arguments
  if (!a || !Object.keys(a).length) return `${name}()`
  try { return `${name}(${JSON.stringify(a)})` } catch { return `${name}(…)` }
})

async function respond(approved: boolean, remember: boolean) {
  if (!confirmation.value?.confirmation_id || !props.systemCompletionId || responding.value) return
  responding.value = true
  try {
    const res = await useMyFetch(
      `/completions/${props.systemCompletionId}/mcp_tool_confirmations/${confirmation.value.confirmation_id}`,
      { method: 'POST', body: { approved, remember } }
    )
    if (res?.error?.value) {
      console.warn('Failed to respond to tool approval', res.error.value)
    } else {
      answered.value = true
    }
  } finally {
    responding.value = false
  }
}

// ── 'auto' policy: small-model review verdict ──────────────────────
// Live runs carry it on the tool_execution (SSE handler); rehydrated runs
// read the persisted copy from result_json.
const autoPolicy = computed(() => {
  const live = (props.toolExecution as any).auto_policy
  if (live) return live
  const rj = resultJson.value as any
  if (rj.policy_verdict) return rj.policy_verdict
  if (rj.blocked_by_policy === 'auto') {
    return { approved: false, reason: rj.policy_reason || '' }
  }
  return null
})
const toolName = computed(() => props.toolExecution?.tool_name || '')
const args = computed(() => props.toolExecution?.arguments_json || {})
const resultJson = computed(() => props.toolExecution?.result_json || {})

const isExecuteMcp = computed(() => toolName.value === 'execute_mcp')

// The MCP/API connection this call ran against, resolved from the report's
// data sources by the streamed/persisted connection_name (or the raw
// connection_id argument, which accepts name or id).
const mcpConnection = computed(() => {
  if (!isExecuteMcp.value) return null
  const target = resultJson.value.connection_name || args.value.connection_id
  if (!target) return null
  for (const ds of props.dataSources || []) {
    for (const c of ds.connections || []) {
      if (c.type !== 'mcp' && c.type !== 'custom_api') continue
      if (c.name === target || c.id === target) return c
    }
  }
  return null
})

// Catalog key ("monday", "notion", …) so known connectors get their brand
// icon; custom MCP servers have none and fall back to the MCP logo.
const connectorKey = computed(() =>
  mcpConnection.value?.connector_key || mcpConnection.value?.config?.catalog_key || null
)

const duration = computed(() => {
  const ms = props.toolExecution?.duration_ms
  if (!ms) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
})

// Model-authored, human-readable label for this call (e.g. "Searching Notion
// for churned customers"). When present it replaces the mechanical
// connection/tool-name label in the header — the brand icon still renders.
const modelTitle = computed<string>(() => {
  const tt = args.value?.title
  return typeof tt === 'string' && tt.trim() ? tt.trim() : ''
})

const runningLabel = computed(() => {
  if (toolName.value === 'execute_mcp' && ['awaiting_confirmation', 'awaiting_approval'].includes(progressStage.value)) {
    const label = t('tools.mcp.awaitingApproval', { name: args.value.tool_name || 'tool' })
    const conn = confirmation.value?.connection_name
    return conn ? `${label} · ${conn}` : label
  }
  if (toolName.value === 'execute_mcp' && progressStage.value === 'auto_policy_review') {
    return t('tools.mcp.autoReviewing', { name: args.value.tool_name || 'tool' })
  }
  if (modelTitle.value) return modelTitle.value
  if (toolName.value === 'search_mcps') return t('tools.mcp.searching')
  if (toolName.value === 'execute_mcp') {
    const connName = resultJson.value.connection_name
    const label = connName || args.value.tool_name || 'MCP tool'
    return t('tools.mcp.callingTool', { name: label })
  }
  if (toolName.value === 'write_csv') return t('tools.mcp.writingCsv')
  return t('tools.mcp.running')
})

const doneLabel = computed(() => {
  if (toolName.value === 'search_mcps') {
    // Prefer the model's label but keep the useful result count suffix.
    if (modelTitle.value) {
      const count = resultJson.value.total_count ?? resultJson.value.tools?.length
      return count != null ? `${modelTitle.value} (${count})` : modelTitle.value
    }
    const count = resultJson.value.total_count ?? resultJson.value.tools?.length ?? 0
    return t('tools.mcp.foundTools', { count })
  }
  if (toolName.value === 'execute_mcp') {
    // On failure always surface the failed state; otherwise prefer the label.
    if (resultJson.value.success === false) {
      const connName = resultJson.value.connection_name || args.value.tool_name || 'MCP tool'
      const blocked = resultJson.value.blocked_by_policy
      if (blocked === 'ask') return t('tools.mcp.declined', { name: args.value.tool_name || connName })
      if (blocked === 'auto') return t('tools.mcp.autoDeclinedLabel', { name: args.value.tool_name || connName })
      if (blocked) return t('tools.mcp.blockedByPolicy', { name: args.value.tool_name || connName })
      return t('tools.mcp.failed', { name: connName })
    }
    if (modelTitle.value) return modelTitle.value
    const connName = resultJson.value.connection_name || args.value.tool_name || 'MCP tool'
    if (resultJson.value.file_id) return t('tools.mcp.csvSuccess', { name: connName })
    return `${connName}`
  }
  if (modelTitle.value) return modelTitle.value
  if (toolName.value === 'write_csv') {
    const rows = resultJson.value.row_count
    return rows ? t('tools.common.rows', { n: rows }) : 'CSV'
  }
  return 'MCP tool'
})

// The actual call being made — surfaced so users can see WHAT was invoked,
// not just the result. execute_mcp: the underlying tool + its arguments.
// search_mcps / write_csv: the relevant query/code input.
const command = computed(() => {
  const a = args.value || {}
  if (toolName.value === 'execute_mcp') {
    const called = a.tool_name
    if (!called) return ''
    const toolArgs = a.arguments
    if (toolArgs && Object.keys(toolArgs).length) {
      return `${called}(${JSON.stringify(toolArgs, null, 2)})`
    }
    return `${called}()`
  }
  if (toolName.value === 'search_mcps') {
    return a.query ? `query: ${a.query}` : ''
  }
  if (toolName.value === 'write_csv') {
    return a.code || ''
  }
  return ''
})

const preview = computed(() => {
  const rj = resultJson.value
  // search_mcps: show tool list
  if (toolName.value === 'search_mcps' && Array.isArray(rj.tools)) {
    return rj.tools.map((t: any) => `${t.name} — ${t.description}`).join('\n')
  }
  // execute_mcp: show preview data
  if (rj.preview) {
    return typeof rj.preview === 'string' ? rj.preview : JSON.stringify(rj.preview, null, 2)
  }
  // write_csv: show execution log
  if (rj.execution_log) return rj.execution_log
  // fallback
  if (rj.details) return rj.details
  return ''
})

const errorMessage = computed(() => resultJson.value.error_message || '')

function toggleExpanded() {
  if (status.value !== 'running') {
    isExpanded.value = !isExpanded.value
  }
}
</script>

<style scoped>
@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
  font-weight: 400;
  opacity: 1;
}

.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}

.slide-enter-active, .slide-leave-active {
  transition: all 0.15s ease;
  overflow: hidden;
}
.slide-enter-from, .slide-leave-to {
  opacity: 0;
  max-height: 0;
}
.slide-enter-to, .slide-leave-from {
  opacity: 1;
  max-height: 300px;
}
</style>
