<template>
  <div class="mt-1">
    <RemoteShellTool v-if="isShell" :args="args.arguments || {}" :result="resultJson" :status="status"
      :progress-stage="progressStage" :run-status="runStatus" :duration="duration"
      :connection-name="resultJson.connection_name" :approval-label="approvalOutcomeLabel"
      :confirmation-target="confirmation?.arguments?._execution_target" />
    <!-- Status header -->
    <Transition v-if="!isShell" name="fade" appear>
      <div
        class="flex items-center text-chat-2 text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="flex items-center gap-1">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-ink-muted" />
          <span class="tool-shimmer">{{ runningLabel }}</span>
        </span>
        <span v-else class="text-gray-600 dark:text-gray-400 flex items-center gap-1">
          <DataSourceIcon v-if="connectionIcon" :type="connectionIcon.type" :connector-key="connectionIcon.connectorKey" class="w-3 h-3 me-1 shrink-0" />
          <McpIcon v-else-if="isMcpFamily" class="w-3 h-3 me-1 shrink-0" />
          <Icon v-else name="heroicons-server-stack" class="w-3 h-3 me-1 text-ink-muted" />
          <span>{{ doneLabel }}</span>
          <span v-if="duration" class="text-ink-muted ms-1">{{ duration }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-ink-muted rtl-flip"
          />
        </span>
      </div>
    </Transition>

    <!-- MCP App: the interactive UI the server attached to this tool. Shown
         open, under the status line — it is the result, not a detail. -->
    <McpAppFrame
      v-if="!isShell && mcpApp && appHost && props.toolExecution?.id && status !== 'running'"
      :tool-execution-id="props.toolExecution.id"
      :status="status"
      :connection-name="resultJson.connection_name"
    />

    <!-- Approval prompt ('ask' policy): the run is paused on this call.
         Deliberately quiet — an indented block under the status line, a
         one-line argument echo, and a row of small actions. -->
    <div
      v-if="showApprovalCard"
      class="mt-1.5 ms-1 border-s-2 border-amber-300/70 dark:border-amber-600/50 ps-2.5 py-0.5 space-y-1.5"
      data-testid="mcp-approval-card"
    >
      <button
        v-if="argsOneLine && !isShell"
        type="button"
        class="block max-w-full truncate text-start text-chat-2 font-mono text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
        :title="showArgs ? '' : argsOneLine"
        @click="showArgs = !showArgs"
      >{{ argsOneLine }}</button>
      <div v-if="isShell" class="text-chat-2 text-ink-muted">
        {{ t('tools.shell.server') }}: <bdi dir="ltr">{{ confirmation?.arguments?._execution_target?.server || resultJson.connection_name }}</bdi>
        · {{ t('tools.shell.directory') }}: <bdi dir="ltr">{{ args.arguments?.working_directory }}</bdi>
      </div>
      <pre
        v-if="showArgs || shellPermissions || mandatoryApproval"
        class="max-h-32 overflow-auto rounded bg-gray-50 dark:bg-gray-900 text-chat-2 leading-tight text-gray-600 dark:text-gray-400 p-2 m-0 whitespace-pre-wrap break-words font-mono"
        :dir="isShell ? 'ltr' : undefined"
      >{{ isShell ? args.arguments?.command : confirmationArgs }}</pre>
      <div v-if="shellPermissions && !mandatoryApproval" class="space-y-1 text-chat-2">
        <label class="text-ink-muted">{{ t('tools.remoteShell.pattern') }}</label>
        <UTextarea dir="ltr" :rows="2" v-model="commandPattern" :aria-label="t('tools.remoteShell.pattern')" class="font-mono" />
        <USelect v-model="commandMatch" :options="[{ value: 'exact', label: t('tools.remoteShell.exact') }, { value: 'glob', label: t('tools.remoteShell.glob') }]" />
        <p class="text-ink-muted">{{ t('tools.remoteShell.hint') }}</p>
      </div>
      <div class="flex flex-wrap items-center gap-1.5 text-chat-2">
        <button
          class="px-2 py-0.5 rounded font-medium text-white bg-blue-600 hover:bg-blue-700 transition-colors disabled:opacity-50"
          :disabled="responding" @click="respond(true, false)"
        >{{ t('tools.mcp.allowOnce') }}</button>
        <button
          v-if="!mandatoryApproval"
          class="px-2 py-0.5 rounded text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-gray-700 dark:hover:text-gray-300 transition-colors disabled:opacity-50"
          :disabled="responding" :title="t('tools.mcp.approvalHint')" @click="respond(true, true)"
        >{{ t(shellPermissions ? 'tools.remoteShell.always' : 'tools.mcp.alwaysAllow') }}</button>
        <UButton v-if="shellPermissions && !mandatoryApproval && confirmation?.arguments?._execution_target?.session_permissions"
          variant="ghost" size="xs" :disabled="responding" @click="respond(true, true, 'session')">{{ t('tools.remoteShell.session') }}</UButton>
        <span class="h-3 w-px bg-gray-200 dark:bg-gray-700"></span>
        <button
          class="px-2 py-0.5 rounded text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-gray-700 dark:hover:text-gray-300 transition-colors disabled:opacity-50"
          :disabled="responding" @click="respond(false, false)"
        >{{ t('tools.mcp.denyOnce') }}</button>
        <button
          v-if="!mandatoryApproval"
          class="px-2 py-0.5 rounded text-ink-muted hover:bg-red-50 dark:hover:bg-red-950 hover:text-red-600 dark:hover:text-red-400 transition-colors disabled:opacity-50"
          :disabled="responding" :title="t('tools.mcp.approvalHint')" @click="respond(false, true)"
        >{{ t('tools.mcp.alwaysDeny') }}</button>
      </div>
      <div
        v-if="respondError"
        class="flex items-start gap-1 text-chat-2 text-amber-600 dark:text-amber-500"
        data-testid="mcp-approval-error"
      >
        <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 mt-0.5 shrink-0" />
        <span class="break-words">{{ respondError }}</span>
      </div>
    </div>

    <!-- 'ask' policy outcome: who allowed or denied this call. Persisted on
         the tool output, so it survives rehydration; right after a click the
         local decision renders as "by you" until the output arrives. -->
    <div
      v-if="!isShell && isExpanded && approvalOutcome"
      class="mt-1 flex items-center gap-1 text-chat-2"
      :class="approvalOutcome.approved ? 'text-ink-muted' : 'text-amber-600 dark:text-amber-500'"
      data-testid="mcp-approval-outcome"
    >
      <Icon :name="approvalOutcome.approved ? 'heroicons-check-circle' : 'heroicons-no-symbol'" class="w-3 h-3 shrink-0" />
      <span class="truncate">{{ approvalOutcomeLabel }}</span>
    </div>

    <!-- Auto policy verdict ('auto' policy): small-model review outcome -->
    <div
      v-if="!isShell && isExpanded && autoPolicy"
      class="mt-1 flex items-center gap-1 text-chat-2"
      :class="autoPolicy.approved ? 'text-ink-muted' : 'text-amber-600 dark:text-amber-500'"
      data-testid="mcp-auto-policy"
    >
      <Icon :name="autoPolicy.approved ? 'heroicons-shield-check' : 'heroicons-shield-exclamation'" class="w-3 h-3 shrink-0" />
      <span class="truncate">{{ autoPolicy.approved ? t('tools.mcp.autoApproved') : t('tools.mcp.autoDenied') }}<template v-if="autoPolicy.reason"> — {{ autoPolicy.reason }}</template></span>
    </div>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="!isShell && isExpanded && status !== 'running'" class="mt-2 space-y-1.5">
        <!-- Command (input) -->
        <div v-if="command" class="group">
          <div
            class="flex items-center text-chat-2 text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-600 dark:hover:text-gray-300 mb-0.5"
            @click="showCommand = !showCommand"
          >
            <Icon
              :name="showCommand ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
              class="w-2.5 h-2.5 me-1 text-ink-muted rtl-flip"
            />
            <span>{{ t('tools.common.input') }}</span>
          </div>
          <div v-if="showCommand" class="max-h-28 overflow-auto rounded bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800">
            <pre class="text-chat-2 leading-tight text-gray-600 dark:text-gray-400 p-2 m-0 whitespace-pre-wrap break-words font-mono">{{ command }}</pre>
          </div>
        </div>

        <!-- Result preview -->
        <div v-if="preview" class="group">
          <div
            class="flex items-center text-chat-2 text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-600 dark:hover:text-gray-300 mb-0.5"
            @click="showPreview = !showPreview"
          >
            <Icon
              :name="showPreview ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
              class="w-2.5 h-2.5 me-1 text-ink-muted rtl-flip"
            />
            <span>{{ t('tools.common.output') }}</span>
            <span v-if="previewMeta" class="ms-1.5 text-ink-muted">{{ previewMeta }}</span>
          </div>
          <div
            v-if="showPreview"
            class="overflow-auto rounded bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800"
            :class="previewFull ? 'max-h-[32rem]' : 'max-h-56'"
          >
            <pre class="text-chat-2 leading-tight text-gray-600 dark:text-gray-400 p-2 m-0 whitespace-pre-wrap break-words font-mono">{{ preview }}</pre>
          </div>
          <!-- The output now carries the whole result, not three sample rows,
               so a fixed 7rem box scrolls a full response through a slit. -->
          <button
            v-if="showPreview && previewIsLong"
            type="button"
            class="mt-0.5 text-chat-2 text-ink-muted hover:text-gray-600 dark:hover:text-gray-300"
            @click="previewFull = !previewFull"
          >{{ previewFull ? t('tools.common.collapse') : t('tools.common.expand') }}</button>
        </div>

        <!-- Error message (amber: a failed tool call is recoverable context) -->
        <div v-if="errorMessage" class="text-chat-2 text-amber-600 dark:text-amber-500 bg-amber-50/60 dark:bg-amber-950/40 rounded px-2 py-1">
          {{ errorMessage }}
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue'
import RemoteShellTool from '~/components/tools/RemoteShellTool.vue'
import { toolOutcome } from '~/utils/toolOutcome'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useToolConnectionIcon } from '~/composables/useToolConnectionIcon'
import McpIcon from '~/components/icons/McpIcon.vue'
import McpAppFrame from '~/components/tools/McpAppFrame.vue'
import Spinner from '~/components/Spinner.vue'
const { t } = useResponseI18n()

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
  runStatus?: string
}>()

const isExpanded = ref(false)
const showPreview = ref(true)
const showCommand = ref(true)

const status = computed(() => props.toolExecution?.status || '')

// ── 'ask' policy: mid-run approval card ─────────────────────────────
const confirmation = computed(() => (props.toolExecution as any).confirmation || null)
const shellPermissions = computed(() => !!confirmation.value?.arguments?._execution_target?.shell_permissions)
const commandPattern = ref('')
const commandMatch = ref('exact')
watch(() => confirmation.value?.confirmation_id, () => {
  commandPattern.value = confirmation.value?.arguments?.command || ''
  commandMatch.value = 'exact'
}, { immediate: true })
const mandatoryApproval = computed(() => !!confirmation.value?.arguments?._execution_target?.mandatory_approval)
const progressStage = computed(() => (props.toolExecution as any).progress_stage || '')
const answered = ref(false)
const responding = ref(false)

const showApprovalCard = computed(() =>
  !!confirmation.value?.confirmation_id &&
  !answered.value &&
  status.value === 'running' &&
  ['awaiting_confirmation', 'awaiting_approval'].includes(progressStage.value)
)

// The clicker's own decision, kept so the outcome shows instantly ("by you")
// while the run resumes and before the persisted output arrives.
const localDecision = ref<{ approved: boolean } | null>(null)

// Persisted 'ask' outcome from the tool output (survives rehydration), with
// the local click as fallback. A timed-out approval is not a decision.
const approvalOutcome = computed(() => {
  const a = (resultJson.value as any).approval
  if (a && !a.timed_out && !a.cancelled) return { approved: !!a.approved, name: a.resolved_by_name || '' }
  if (localDecision.value) return { approved: localDecision.value.approved, name: '' }
  return null
})

const approvalOutcomeLabel = computed(() => {
  const o = approvalOutcome.value
  if (!o) return ''
  if (o.name) return o.approved ? t('tools.mcp.allowedBy', { name: o.name }) : t('tools.mcp.deniedBy', { name: o.name })
  return o.approved ? t('tools.mcp.allowedByYou') : t('tools.mcp.deniedByYou')
})

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

// A failed response must be visible: silently swallowing it made the buttons
// look dead while the run waited out its approval timeout.
const respondError = ref('')

const { markConfirmationAnswered } = useToolConfirmations()

async function respond(approved: boolean, remember: boolean, scope = 'user') {
  if (!confirmation.value?.confirmation_id || !props.systemCompletionId || responding.value) return
  responding.value = true
  respondError.value = ''
  try {
    const res = await useMyFetch(
      `/completions/${props.systemCompletionId}/mcp_tool_confirmations/${confirmation.value.confirmation_id}`,
      { method: 'POST', body: { approved, remember, ...(shellPermissions.value ? { permission_scope: remember ? scope : 'once', command_pattern: commandPattern.value, command_match: commandMatch.value } : {}) } }
    )
    if (res?.error?.value) {
      const err = res.error.value as any
      console.warn('Failed to respond to tool approval', err)
      respondError.value = err?.statusCode === 410
        ? t('tools.mcp.approvalExpired')
        : t('tools.mcp.approvalFailed')
    } else {
      answered.value = true
      markConfirmationAnswered(confirmation.value?.confirmation_id)
      localDecision.value = { approved }
    }
  } catch (e) {
    console.warn('Failed to respond to tool approval', e)
    respondError.value = t('tools.mcp.approvalFailed')
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
// The raw name: execute_mcp after the server's rewrite, but a native
// mcp__<connection>__<tool> or call_tool while it streams. All three render
// as execute_mcp, with the arguments mapped to its shape.
const rawToolName = computed(() => props.toolExecution?.tool_name || '')
const toolName = computed(() => {
  const n = rawToolName.value
  if (n.startsWith('mcp__') || n === 'call_tool') return 'execute_mcp'
  if (n === 'search_tools') return 'search_mcps'
  return n
})
const nativeToolPart = (name: string) => {
  const parts = (name || '').split('__')
  return parts.length >= 3 ? parts.slice(2).join('__') : name
}
const args = computed(() => {
  const a = (props.toolExecution?.arguments_json || {}) as any
  const n = rawToolName.value
  if (n.startsWith('mcp__')) return { tool_name: nativeToolPart(n), arguments: a }
  if (n === 'call_tool') return { tool_name: nativeToolPart(a.name || ''), arguments: a.arguments || {}, title: a.title }
  return a
})
const resultJson = computed(() => props.toolExecution?.result_json || {})
// Set by execute_mcp when the tool renders into an MCP App (ui:// resource).
// Only the signed-in report page hosts apps (it provides 'mcpAppHost'); a
// public conversation shows the tool's text result.
const appHost = inject('mcpAppHost', null)
const mcpApp = computed(() => {
  const app = (resultJson.value as any).mcp_app
  return app && typeof app === 'object' && app.resource_uri ? app : null
})

const isExecuteMcp = computed(() => toolName.value === 'execute_mcp')
// Every MCP-family row (tool discovery + execution) should get at least the MCP
// logo when no brand icon resolves — not the generic server-stack glyph.
const isMcpFamily = computed(() =>
  ['execute_mcp', 'search_mcps', 'write_csv'].includes(toolName.value)
)

// The MCP/API connection this row is about — resolved from the report's data
// sources via the shared resolver (handles execute_mcp's connection_name /
// connection_id, search_mcps' connection_ids, and a sole-provider fallback so
// "Finding <provider> tools" discovery rows get the brand icon too).
const mcpConnIcon = useToolConnectionIcon(
  () => ({ ...props.toolExecution, arguments_json: args.value }),
  () => props.dataSources,
  { connectionTypes: ['mcp', 'custom_api', 'remote_ssh', 'remote_smb'] },
)

// Catalog key ("monday", "notion", …) so known connectors get their brand
// icon; custom MCP servers have none and fall back to the MCP logo.
const connectionIcon = computed(() => resultJson.value.connection_type
  ? { type: resultJson.value.connection_type, connectorKey: mcpConnIcon.value?.connectorKey || null }
  : mcpConnIcon.value)
const isShell = computed(() => args.value.tool_name === 'execute_shell' && (
  connectionIcon.value?.type === 'remote_ssh' || shellPermissions.value
))

const duration = computed(() => {
  const ms = props.toolExecution?.duration_ms
  if (!ms) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${Math.round(ms / 1000)}s`
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
    const label = args.value.tool_name || resultJson.value.connection_name || 'MCP tool'
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
      if (blocked === 'ask') return `${args.value.tool_name || connName} · ${t(`tools.shell.${toolOutcome(resultJson.value, status.value, progressStage.value, props.runStatus)}`)}`
      if (blocked === 'auto') return t('tools.mcp.autoDeclinedLabel', { name: args.value.tool_name || connName })
      if (blocked) return t('tools.mcp.blockedByPolicy', { name: args.value.tool_name || connName })
      return t('tools.mcp.failed', { name: connName })
    }
    if (modelTitle.value) return modelTitle.value
    // The tool that ran, with its connection: "find_artist · music-store".
    const called = args.value.tool_name
    const connName = resultJson.value.connection_name
    return [called, connName].filter(Boolean).join(' · ') || 'MCP tool'
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

const previewFull = ref(false)
const previewIsLong = computed(() => (preview.value?.split('\n').length || 0) > 14)

// "12 of 486 records" — the counts live on the output; showing them here is
// what tells a reader the panel holds a slice rather than the whole response.
const previewMeta = computed(() => {
  const rj = resultJson.value
  if (!isExecuteMcp.value) return ''
  const total = rj.row_count
  if (total == null) return ''
  if (rj.preview_truncated && rj.preview_row_count != null) {
    return t('tools.mcp.recordsPartial', { shown: rj.preview_row_count, total })
  }
  return t('tools.mcp.records', { n: total })
})

const errorMessage = computed(() => resultJson.value.blocked_by_policy === 'ask' && resultJson.value.approval
  ? t(`tools.shell.${toolOutcome(resultJson.value, status.value, progressStage.value, props.runStatus)}`)
  : resultJson.value.error_message || resultJson.value.error || '')

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
