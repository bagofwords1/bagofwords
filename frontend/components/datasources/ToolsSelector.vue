<template>
  <div class="w-full">
    <!-- Header -->
    <div v-if="showHeader" class="mb-4 flex items-center justify-between">
      <div>
        <h1 class="text-lg font-semibold">External Tools</h1>
        <p class="text-gray-500 dark:text-gray-400 text-sm">External tools available to the AI agent for this data source.</p>
      </div>
      <div v-if="canUpdate" class="flex items-center gap-2">
        <UButton
          color="blue"
          variant="solid"
          size="xs"
          icon="i-heroicons-plus"
          @click="$emit('add-mcp')"
        >
          Add MCP
        </UButton>
        <UButton
          color="blue"
          variant="outline"
          size="xs"
          icon="i-heroicons-plus"
          @click="$emit('add-custom-api')"
        >
          Add Custom API
        </UButton>
      </div>
    </div>

    <!-- Empty state -->
    <div v-if="!loading && connections.length === 0" class="py-16 text-center border border-dashed border-gray-200 dark:border-gray-700 rounded-lg">
      <UIcon name="i-heroicons-server-stack" class="w-10 h-10 mx-auto text-gray-300 dark:text-gray-600 mb-3" />
      <p class="text-sm text-gray-500 dark:text-gray-400 mb-1">No tool connections yet</p>
      <p class="text-xs text-gray-400 dark:text-gray-600 mb-4">Connect an MCP server or custom API to give the AI agent access to external tools.</p>
      <div v-if="canUpdate" class="flex items-center justify-center gap-2">
        <UButton color="blue" variant="soft" size="xs" icon="i-heroicons-plus" @click="$emit('add-mcp')">
          Add MCP Server
        </UButton>
        <UButton color="blue" variant="soft" size="xs" icon="i-heroicons-plus" @click="$emit('add-custom-api')">
          Add Custom API
        </UButton>
      </div>
    </div>

    <!-- Loading -->
    <div v-else-if="loading" class="text-sm text-gray-500 dark:text-gray-400 py-10 flex items-center justify-center">
      <Spinner class="w-4 h-4 me-2" />
      Loading tools...
    </div>

    <!-- Connections with tools -->
    <div v-else class="space-y-4">
      <div v-for="conn in connections" :key="conn.id" class="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
        <!-- Connection header -->
        <div class="flex items-center justify-between px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700">
          <div class="flex items-center gap-2">
            <span class="text-sm font-medium text-gray-900 dark:text-white">{{ conn.name }}</span>
            <span class="text-[9px] px-1.5 py-0.5 rounded-full bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-400 uppercase font-medium tracking-wide">{{ conn.type === 'custom_api' ? 'API' : 'MCP' }}</span>
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-[10px] text-gray-400 dark:text-gray-600 me-1">
              {{ getEnabledCount(conn.id) }}/{{ getToolCount(conn.id) }} enabled
            </span>
            <UTooltip v-if="canUpdate" text="Refresh tools">
              <button
                @click="refreshTools(conn.id)"
                :disabled="refreshingConn === conn.id"
                class="p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-700 disabled:opacity-50"
              >
                <Spinner v-if="refreshingConn === conn.id" class="w-3.5 h-3.5" />
                <UIcon v-else name="heroicons-arrow-path" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-600" />
              </button>
            </UTooltip>
            <UTooltip v-if="canUpdate" text="Edit connection">
              <button
                @click="$emit('edit-connection', conn)"
                class="p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-700"
              >
                <UIcon name="heroicons-pencil-square" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-600" />
              </button>
            </UTooltip>
            <UTooltip v-if="canUpdate" text="Remove connection">
              <button
                @click="$emit('delete-connection', conn)"
                class="p-1 rounded hover:bg-red-50 dark:hover:bg-red-950"
              >
                <UIcon name="heroicons-trash" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-600 hover:text-red-500" />
              </button>
            </UTooltip>
          </div>
        </div>

        <!-- Search (only if > 5 tools) -->
        <div v-if="getToolCount(conn.id) > 5" class="px-4 pt-2">
          <input
            v-model="searchQuery"
            type="text"
            placeholder="Filter tools..."
            class="border border-gray-200 dark:border-gray-700 rounded px-2 py-1 w-full max-w-xs h-7 text-xs focus:outline-none focus:border-blue-400"
          />
        </div>

        <!-- Select-all + bulk actions. A connection can expose dozens of tools;
             setting each one individually is a click per tool per column. -->
        <div
          v-if="getFilteredTools(conn.id).length > 0"
          class="flex flex-wrap items-center gap-2 px-4 py-1.5 text-xs border-b border-gray-100 dark:border-gray-800"
          :class="selectedCount(conn.id) > 0 ? 'bg-blue-50/60 dark:bg-blue-950/30' : ''"
          data-testid="tools-bulk-bar"
        >
          <UCheckbox
            color="blue"
            :model-value="allSelected(conn.id)"
            :indeterminate="someSelected(conn.id)"
            :aria-label="$t('toolsSelector.selectAll')"
            data-testid="tools-select-all"
            @update:model-value="(val: boolean) => toggleSelectAll(conn.id, val)"
          />
          <span v-if="selectedCount(conn.id) === 0" class="text-[11px] text-gray-400 dark:text-gray-600">
            {{ $t('toolsSelector.selectAll') }}
          </span>
          <template v-else>
            <span class="text-[11px] font-medium text-gray-600 dark:text-gray-300" data-testid="tools-selected-count">
              {{ $t('toolsSelector.nSelected', { count: selectedCount(conn.id) }) }}
            </span>
            <template v-if="canUpdate">
              <span class="h-3 w-px bg-gray-200 dark:bg-gray-700"></span>
              <button
                class="px-1.5 py-0.5 rounded text-gray-600 dark:text-gray-300 hover:bg-white dark:hover:bg-gray-800 disabled:opacity-50"
                :disabled="bulkBusy"
                data-testid="tools-bulk-enable"
                @click="bulkSetEnabled(conn.id, true)"
              >{{ $t('toolsSelector.bulkEnable') }}</button>
              <button
                class="px-1.5 py-0.5 rounded text-gray-600 dark:text-gray-300 hover:bg-white dark:hover:bg-gray-800 disabled:opacity-50"
                :disabled="bulkBusy"
                data-testid="tools-bulk-disable"
                @click="bulkSetEnabled(conn.id, false)"
              >{{ $t('toolsSelector.bulkDisable') }}</button>
              <span class="h-3 w-px bg-gray-200 dark:bg-gray-700"></span>
              <span class="text-[9px] uppercase tracking-wide text-gray-300 dark:text-gray-600">{{ $t('toolsSelector.adminPolicy') }}</span>
              <select
                :value="''"
                :disabled="bulkBusy"
                class="text-[10px] border border-gray-200 dark:border-gray-700 rounded px-1 py-0.5 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-400 focus:outline-none focus:border-blue-400 disabled:opacity-50"
                data-testid="tools-bulk-policy"
                @change="(e: Event) => bulkSetPolicy(conn.id, (e.target as HTMLSelectElement))"
              >
                <option value="">{{ $t('toolsSelector.bulkSetPolicy') }}</option>
                <option value="allow">{{ $t('toolsSelector.policyAllow') }}</option>
                <option value="ask">{{ $t('toolsSelector.policyAsk') }}</option>
                <option value="auto">{{ $t('toolsSelector.policyAuto') }}</option>
                <option value="deny">{{ $t('toolsSelector.policyDeny') }}</option>
              </select>
              <button
                class="px-1.5 py-0.5 rounded text-gray-500 dark:text-gray-400 hover:bg-white dark:hover:bg-gray-800 disabled:opacity-50"
                :disabled="bulkBusy"
                :title="$t('toolsSelector.resetTip')"
                data-testid="tools-bulk-reset"
                @click="bulkReset(conn.id)"
              >{{ $t('toolsSelector.reset') }}</button>
            </template>
            <span class="h-3 w-px bg-gray-200 dark:bg-gray-700"></span>
            <span class="text-[9px] uppercase tracking-wide text-gray-300 dark:text-gray-600">{{ $t('toolsSelector.myPolicy') }}</span>
            <select
              :value="''"
              :disabled="bulkBusy"
              class="text-[10px] border border-gray-200 dark:border-gray-700 rounded px-1 py-0.5 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-400 focus:outline-none focus:border-blue-400 disabled:opacity-50"
              data-testid="tools-bulk-my-policy"
              @change="(e: Event) => bulkSetMyPolicy(conn.id, (e.target as HTMLSelectElement))"
            >
              <option value="">{{ $t('toolsSelector.bulkSetPolicy') }}</option>
              <option value="__clear__">{{ $t('toolsSelector.bulkClearMyPolicy') }}</option>
              <option value="allow">{{ $t('toolsSelector.policyAllow') }}</option>
              <option value="ask">{{ $t('toolsSelector.policyAsk') }}</option>
              <option value="auto">{{ $t('toolsSelector.policyAuto') }}</option>
              <option value="deny">{{ $t('toolsSelector.policyDeny') }}</option>
            </select>
            <button
              class="ms-auto px-1.5 py-0.5 rounded text-gray-400 dark:text-gray-600 hover:text-gray-600 dark:hover:text-gray-400"
              data-testid="tools-bulk-clear"
              @click="clearSelection(conn.id)"
            >{{ $t('toolsSelector.clearSelection') }}</button>
            <Spinner v-if="bulkBusy" class="w-3 h-3" />
          </template>
        </div>

        <!-- Tool list -->
        <ul v-if="getFilteredTools(conn.id).length > 0" class="divide-y divide-gray-100 dark:divide-gray-800">
          <li
            v-for="tool in getFilteredTools(conn.id)"
            :key="tool.id"
            class="px-4 py-2 hover:bg-gray-50/50 dark:hover:bg-gray-800 transition-colors"
          >
            <div class="flex items-center gap-3">
              <!-- Row selection (feeds the bulk bar above). The enabled state
                   moved to a toggle on the right so the two aren't confused. -->
              <UCheckbox
                color="blue"
                :model-value="!!selected[tool.id]"
                :aria-label="$t('toolsSelector.selectTool', { name: tool.name })"
                data-testid="tool-select"
                @update:model-value="(val: boolean) => setSelected(tool.id, val)"
              />
              <button
                type="button"
                class="flex items-center gap-1.5 text-start flex-shrink-0"
                @click="toggleExpand(tool.id)"
              >
                <UIcon
                  :name="expandedTools[tool.id] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
                  class="w-3 h-3 text-gray-400 dark:text-gray-600 flex-shrink-0 rtl-flip"
                />
                <code class="text-[13px] text-gray-800 dark:text-gray-200 font-medium whitespace-nowrap">{{ tool.name }}</code>
                <!-- HTTP identity for custom_api tools: method chip + path -->
                <span
                  v-if="tool.metadata?.method"
                  class="text-[9px] px-1 py-0.5 rounded font-semibold tracking-wide"
                  :class="methodChipClass(tool.metadata.method)"
                  data-testid="tool-http-method"
                >{{ tool.metadata.method }}</span>
                <code
                  v-if="tool.metadata?.path"
                  class="text-[10px] text-gray-400 dark:text-gray-600 whitespace-nowrap hidden sm:inline"
                  data-testid="tool-http-path"
                >{{ tool.metadata.path }}</code>
                <span v-if="!tool.is_enabled" class="text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-600">off</span>
                <span v-if="canUpdate && !tool.has_overlay" class="text-[9px] px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-950 text-blue-500" title="Inherits connection default">default</span>
              </button>
              <span class="text-[11px] text-gray-400 dark:text-gray-600 truncate min-w-0">{{ tool.description }}</span>
              <div class="flex items-center gap-2 ms-auto flex-shrink-0">
                <!-- Enabled: the admin on/off switch for this tool -->
                <UToggle
                  v-if="canUpdate"
                  color="blue"
                  size="2xs"
                  :model-value="tool.is_enabled"
                  :aria-label="$t('toolsSelector.enabledTip')"
                  :title="$t('toolsSelector.enabledTip')"
                  data-testid="tool-enabled-toggle"
                  @update:model-value="(val: boolean) => toggleTool(conn.id, tool.id, val)"
                />
                <!-- Admin policy: editable for agent admins, read-only badge otherwise -->
                <div class="flex items-center gap-1" :title="$t('toolsSelector.adminPolicyTip')">
                  <span class="text-[9px] uppercase tracking-wide text-gray-300 dark:text-gray-600">{{ $t('toolsSelector.adminPolicy') }}</span>
                  <select
                    v-if="canUpdate"
                    :value="tool.policy"
                    @change="(e: Event) => setToolPolicy(conn.id, tool.id, (e.target as HTMLSelectElement).value)"
                    class="text-[10px] border border-gray-200 dark:border-gray-700 rounded px-1 py-0.5 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-400 focus:outline-none focus:border-blue-400"
                  >
                    <option value="allow">{{ $t('toolsSelector.policyAllow') }}</option>
                    <option value="ask">{{ $t('toolsSelector.policyAsk') }}</option>
                    <option value="auto">{{ $t('toolsSelector.policyAuto') }}</option>
                    <option value="deny">{{ $t('toolsSelector.policyDeny') }}</option>
                  </select>
                  <span
                    v-else
                    class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400"
                    data-testid="admin-policy-readonly"
                  >{{ policyLabel(tool.policy) }}</span>
                </div>
                <button
                  v-if="canUpdate && tool.has_overlay"
                  @click="resetTool(conn.id, tool.id)"
                  class="text-[10px] text-gray-400 dark:text-gray-600 hover:text-gray-600 dark:hover:text-gray-400"
                  :title="$t('toolsSelector.resetTip')"
                >{{ $t('toolsSelector.reset') }}</button>
                <!-- Per-user policy: every member controls their own -->
                <div v-if="tool.policy !== 'deny'" class="flex items-center gap-1" :title="$t('toolsSelector.myPolicyTip')">
                  <span class="text-[9px] uppercase tracking-wide text-gray-300 dark:text-gray-600">{{ $t('toolsSelector.myPolicy') }}</span>
                  <select
                    :value="tool.user_policy || ''"
                    @change="(e: Event) => setMyPolicy(conn.id, tool.id, (e.target as HTMLSelectElement).value)"
                    class="text-[10px] border border-gray-200 dark:border-gray-700 rounded px-1 py-0.5 bg-white dark:bg-gray-900 focus:outline-none focus:border-blue-400"
                    :class="tool.user_policy ? 'text-blue-600 dark:text-blue-400 border-blue-200 dark:border-blue-800' : 'text-gray-600 dark:text-gray-400'"
                    data-testid="my-policy-select"
                  >
                    <option value="">{{ $t('toolsSelector.policyInherit', { policy: policyLabel(tool.policy) }) }}</option>
                    <option value="allow">{{ $t('toolsSelector.policyAllow') }}</option>
                    <option value="ask">{{ $t('toolsSelector.policyAsk') }}</option>
                    <option value="auto">{{ $t('toolsSelector.policyAuto') }}</option>
                    <option value="deny">{{ $t('toolsSelector.policyDeny') }}</option>
                  </select>
                </div>
              </div>
            </div>

            <!-- Expanded -->
            <div v-if="expandedTools[tool.id]" class="mt-2 ms-9 space-y-2">
              <div v-if="tool.metadata?.method" class="flex items-center gap-1.5 text-xs">
                <span
                  class="text-[9px] px-1 py-0.5 rounded font-semibold tracking-wide"
                  :class="methodChipClass(tool.metadata.method)"
                >{{ tool.metadata.method }}</span>
                <code class="text-[11px] text-gray-500 dark:text-gray-400">{{ tool.metadata.path }}</code>
              </div>
              <p v-if="tool.description" class="text-xs text-gray-500 dark:text-gray-400">{{ tool.description }}</p>
              <div v-if="tool.input_schema?.properties" class="text-xs">
                <div class="text-[10px] text-gray-400 dark:text-gray-600 uppercase font-medium mb-1">Parameters</div>
                <div class="grid gap-1">
                  <div
                    v-for="(prop, pname) in tool.input_schema.properties"
                    :key="pname"
                    class="flex items-baseline gap-2 text-xs"
                  >
                    <code class="text-[11px] text-blue-700 bg-blue-50 dark:bg-blue-950 px-1 py-0.5 rounded">{{ pname }}</code>
                    <span class="text-gray-400 dark:text-gray-600">{{ prop.type || 'any' }}</span>
                    <span v-if="(tool.input_schema.required || []).includes(pname)" class="text-[9px] text-red-400">required</span>
                    <span v-if="prop.description" class="text-gray-500 dark:text-gray-400 truncate">— {{ prop.description }}</span>
                  </div>
                </div>
              </div>
              <details v-if="tool.input_schema" class="text-[10px] text-gray-400 dark:text-gray-600">
                <summary class="cursor-pointer hover:text-gray-600 dark:hover:text-gray-400">Raw schema</summary>
                <pre class="mt-1 bg-gray-50 dark:bg-gray-900 rounded p-2 text-[10px] font-mono text-gray-500 dark:text-gray-400 overflow-x-auto max-h-32 overflow-y-auto">{{ JSON.stringify(tool.input_schema, null, 2) }}</pre>
              </details>
            </div>
          </li>
        </ul>

        <!-- Empty tools -->
        <div v-else class="px-4 py-6 text-xs text-gray-400 dark:text-gray-600 text-center">
          {{ searchQuery ? 'No matching tools' : 'No tools discovered yet.' }}
          <button v-if="!searchQuery && canUpdate" @click="refreshTools(conn.id)" class="text-blue-500 hover:underline ms-1">Refresh</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const props = withDefaults(defineProps<{
  dsId: string
  connections: any[]
  canUpdate: boolean
  // Hide the "External Tools" heading + Add buttons when embedded in compact
  // surfaces (e.g. the create_agent chat card).
  showHeader?: boolean
}>(), { showHeader: true })

defineEmits(['add-mcp', 'add-custom-api', 'edit-connection', 'delete-connection'])

const toast = useToast()
const { t } = useI18n()

const loading = ref(false)
const refreshingConn = ref<string | null>(null)
const searchQuery = ref('')
const expandedTools = ref<Record<string, boolean>>({})

// Tools keyed by connection ID
const toolsByConnection = ref<Record<string, any[]>>({})

// ── Bulk selection ──────────────────────────────────────────────────
// Flat map keyed by tool id; each connection's bulk bar looks only at the
// tools currently visible in its own (search-filtered) list.
const selected = ref<Record<string, boolean>>({})
const bulkBusy = ref(false)

function setSelected(toolId: string, val: boolean) {
  if (val) selected.value[toolId] = true
  else delete selected.value[toolId]
}

function selectedIds(connectionId: string): string[] {
  return getFilteredTools(connectionId)
    .filter((t: any) => selected.value[t.id])
    .map((t: any) => t.id)
}

function selectedCount(connectionId: string): number {
  return selectedIds(connectionId).length
}

function allSelected(connectionId: string): boolean {
  const visible = getFilteredTools(connectionId)
  return visible.length > 0 && visible.every((t: any) => selected.value[t.id])
}

function someSelected(connectionId: string): boolean {
  const n = selectedCount(connectionId)
  return n > 0 && !allSelected(connectionId)
}

function toggleSelectAll(connectionId: string, val: boolean) {
  for (const t of getFilteredTools(connectionId)) setSelected(t.id, val)
}

function clearSelection(connectionId: string) {
  for (const id of selectedIds(connectionId)) delete selected.value[id]
}

/** Merge rows returned by a batch endpoint back into the local list. */
function applyRows(connectionId: string, rows: any[]) {
  const tools = toolsByConnection.value[connectionId] || []
  for (const row of rows || []) {
    const idx = tools.findIndex((t: any) => t.id === row.id)
    if (idx !== -1) tools[idx] = { ...tools[idx], ...row }
  }
}

async function runBulk(
  connectionId: string,
  fn: (ids: string[]) => Promise<any>,
  successTitle: string,
) {
  const ids = selectedIds(connectionId)
  if (!ids.length || bulkBusy.value) return
  bulkBusy.value = true
  try {
    const response = await fn(ids)
    if (response?.error?.value) throw response.error.value
    applyRows(connectionId, (response?.data?.value as any[]) || [])
    clearSelection(connectionId)
    toast.add({ title: successTitle, color: 'green' })
  } catch (e) {
    console.error('Bulk tool update failed:', e)
    toast.add({ title: t('toolsSelector.bulkFailed'), color: 'red' })
  } finally {
    bulkBusy.value = false
  }
}

function bulkSetEnabled(connectionId: string, enabled: boolean) {
  return runBulk(
    connectionId,
    (tool_ids) => useMyFetch(`/data_sources/${props.dsId}/tools/batch`, {
      method: 'PUT',
      body: { tool_ids, is_enabled: enabled },
    }),
    t(enabled ? 'toolsSelector.bulkEnabled' : 'toolsSelector.bulkDisabled', {
      count: selectedCount(connectionId),
    }),
  )
}

function bulkSetPolicy(connectionId: string, el: HTMLSelectElement) {
  const policy = el.value
  el.value = ''  // the select is an action menu, not a bound value
  if (!policy) return
  return runBulk(
    connectionId,
    (tool_ids) => useMyFetch(`/data_sources/${props.dsId}/tools/batch`, {
      method: 'PUT',
      body: { tool_ids, policy },
    }),
    t('toolsSelector.bulkPolicySet', {
      count: selectedCount(connectionId), policy: policyLabel(policy),
    }),
  )
}

function bulkReset(connectionId: string) {
  return runBulk(
    connectionId,
    (tool_ids) => useMyFetch(`/data_sources/${props.dsId}/tools/batch/reset`, {
      method: 'POST',
      body: { tool_ids },
    }),
    t('toolsSelector.bulkResetDone', { count: selectedCount(connectionId) }),
  )
}

function bulkSetMyPolicy(connectionId: string, el: HTMLSelectElement) {
  const choice = el.value
  el.value = ''
  if (!choice) return
  const policy = choice === '__clear__' ? '' : choice
  return runBulk(
    connectionId,
    (tool_ids) => useMyFetch(`/data_sources/${props.dsId}/tools/batch/my_policy`, {
      method: 'PUT',
      body: { tool_ids, policy },
    }),
    policy
      ? t('toolsSelector.bulkMyPolicySet', {
          count: selectedCount(connectionId), policy: policyLabel(policy),
        })
      : t('toolsSelector.bulkMyPolicyCleared', { count: selectedCount(connectionId) }),
  )
}

onMounted(async () => {
  if (props.connections.length > 0) {
    await loadAllTools()
  }
})

watch(() => props.connections, async (newConns) => {
  if (newConns.length > 0) {
    await loadAllTools()
  }
}, { deep: true })

async function loadAllTools() {
  // One round-trip for all tools across linked connections.
  // Effective state = per-agent overlay merged with connection defaults.
  loading.value = true
  try {
    const response = await useMyFetch(`/data_sources/${props.dsId}/tools`, { method: 'GET' })
    if (response.data.value) {
      const grouped: Record<string, any[]> = {}
      for (const t of response.data.value as any[]) {
        if (!grouped[t.connection_id]) grouped[t.connection_id] = []
        grouped[t.connection_id].push(t)
      }
      toolsByConnection.value = grouped
    }
  } catch (e) {
    console.error('Failed to load agent tools:', e)
  } finally {
    loading.value = false
  }
}

async function refreshTools(connectionId: string) {
  refreshingConn.value = connectionId
  try {
    // Refresh the underlying ConnectionTool discovery (org-level), then
    // reload the agent-scoped view so the new tools show up with their
    // current effective state.
    await useMyFetch(`/connections/${connectionId}/refresh-tools`, { method: 'POST' })
    await loadAllTools()
    toast.add({ title: 'Tools refreshed', color: 'green' })
  } catch (e) {
    toast.add({ title: 'Failed to refresh tools', color: 'red' })
  } finally {
    refreshingConn.value = null
  }
}

async function toggleTool(connectionId: string, toolId: string, enabled: boolean) {
  try {
    const response = await useMyFetch(`/data_sources/${props.dsId}/tools/${toolId}`, {
      method: 'PUT',
      body: { is_enabled: enabled },
    })
    if (response.data.value) {
      const tools = toolsByConnection.value[connectionId] || []
      const idx = tools.findIndex((t: any) => t.id === toolId)
      if (idx !== -1) {
        tools[idx] = response.data.value
      }
    }
  } catch (e) {
    toast.add({ title: 'Failed to update tool', color: 'red' })
  }
}

async function resetTool(connectionId: string, toolId: string) {
  // Remove the per-agent overlay; tool reverts to connection-default state.
  try {
    const response = await useMyFetch(`/data_sources/${props.dsId}/tools/${toolId}`, {
      method: 'DELETE',
    })
    if (response.data.value) {
      const tools = toolsByConnection.value[connectionId] || []
      const idx = tools.findIndex((t: any) => t.id === toolId)
      if (idx !== -1) {
        tools[idx] = response.data.value
      }
    }
  } catch (e) {
    toast.add({ title: 'Failed to reset tool', color: 'red' })
  }
}

async function setToolPolicy(connectionId: string, toolId: string, policy: string) {
  try {
    const response = await useMyFetch(`/data_sources/${props.dsId}/tools/${toolId}`, {
      method: 'PUT',
      body: { policy },
    })
    if (response.data.value) {
      const tools = toolsByConnection.value[connectionId] || []
      const idx = tools.findIndex((t: any) => t.id === toolId)
      if (idx !== -1) {
        tools[idx] = response.data.value
      }
    }
  } catch (e) {
    toast.add({ title: 'Failed to update tool policy', color: 'red' })
  }
}

async function setMyPolicy(connectionId: string, toolId: string, policy: string) {
  // Empty value = inherit the admin policy (delete the personal preference).
  try {
    const response = await useMyFetch(`/data_sources/${props.dsId}/tools/${toolId}/my_policy`, {
      method: policy ? 'PUT' : 'DELETE',
      ...(policy ? { body: { policy } } : {}),
    })
    if (response.data.value) {
      const tools = toolsByConnection.value[connectionId] || []
      const idx = tools.findIndex((t: any) => t.id === toolId)
      if (idx !== -1) {
        tools[idx] = response.data.value
      }
    }
  } catch (e) {
    toast.add({ title: t('toolsSelector.myPolicyFailed'), color: 'red' })
  }
}

function methodChipClass(method: string): string {
  const map: Record<string, string> = {
    GET: 'bg-emerald-50 dark:bg-emerald-950 text-emerald-600 dark:text-emerald-400',
    POST: 'bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-400',
    PUT: 'bg-amber-50 dark:bg-amber-950 text-amber-600 dark:text-amber-400',
    PATCH: 'bg-amber-50 dark:bg-amber-950 text-amber-600 dark:text-amber-400',
    DELETE: 'bg-red-50 dark:bg-red-950 text-red-600 dark:text-red-400',
  }
  return map[(method || '').toUpperCase()] || 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400'
}

function policyLabel(policy: string): string {
  const map: Record<string, string> = {
    allow: t('toolsSelector.policyAllow'),
    ask: t('toolsSelector.policyAsk'),
    confirm: t('toolsSelector.policyAsk'),
    auto: t('toolsSelector.policyAuto'),
    deny: t('toolsSelector.policyDeny'),
  }
  return map[policy] || policy
}

function toggleExpand(toolId: string) {
  expandedTools.value[toolId] = !expandedTools.value[toolId]
}

function getToolCount(connectionId: string): number {
  return (toolsByConnection.value[connectionId] || []).length
}

function getEnabledCount(connectionId: string): number {
  return (toolsByConnection.value[connectionId] || []).filter((t: any) => t.is_enabled).length
}

function getFilteredTools(connectionId: string): any[] {
  const tools = toolsByConnection.value[connectionId] || []
  if (!searchQuery.value.trim()) return tools
  const q = searchQuery.value.toLowerCase().trim()
  return tools.filter((t: any) =>
    t.name?.toLowerCase().includes(q) ||
    t.description?.toLowerCase().includes(q)
  )
}
</script>
