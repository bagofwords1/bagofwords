<template>
  <div class="w-full">
    <!-- Header -->
    <div class="mb-4 flex items-center justify-between">
      <div>
        <h1 class="text-lg font-semibold">External Tools</h1>
        <p class="text-gray-500 text-sm">External tools available to the AI agent for this data source.</p>
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
    <div v-if="!loading && connections.length === 0" class="py-16 text-center border border-dashed border-gray-200 rounded-lg">
      <UIcon name="i-heroicons-server-stack" class="w-10 h-10 mx-auto text-gray-300 mb-3" />
      <p class="text-sm text-gray-500 mb-1">No tool connections yet</p>
      <p class="text-xs text-gray-400 mb-4">Connect an MCP server or custom API to give the AI agent access to external tools.</p>
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
    <div v-else-if="loading" class="text-sm text-gray-500 py-10 flex items-center justify-center">
      <Spinner class="w-4 h-4 me-2" />
      Loading tools...
    </div>

    <!-- Connections with tools -->
    <div v-else class="space-y-4">
      <div v-for="conn in connections" :key="conn.id" class="border border-gray-200 rounded-lg overflow-hidden">
        <!-- Connection header -->
        <div class="flex items-center justify-between px-4 py-2.5 bg-gray-50 border-b border-gray-200">
          <div class="flex items-center gap-2">
            <span class="text-sm font-medium text-gray-900">{{ conn.name }}</span>
            <span class="text-[9px] px-1.5 py-0.5 rounded-full bg-gray-200 text-gray-600 uppercase font-medium tracking-wide">{{ conn.type === 'custom_api' ? 'API' : 'MCP' }}</span>
          </div>
          <div class="flex items-center gap-1.5">
            <span class="text-[10px] text-gray-400 me-1">
              {{ getEnabledCount(conn.id) }}/{{ getToolCount(conn.id) }} enabled
            </span>
            <UTooltip text="Refresh tools">
              <button
                v-if="canUpdate"
                @click="refreshTools(conn.id)"
                :disabled="refreshingConn === conn.id"
                class="p-1 rounded hover:bg-gray-200 disabled:opacity-50"
              >
                <Spinner v-if="refreshingConn === conn.id" class="w-3.5 h-3.5" />
                <UIcon v-else name="heroicons-arrow-path" class="w-3.5 h-3.5 text-gray-400" />
              </button>
            </UTooltip>
            <UTooltip text="Edit connection">
              <button
                v-if="canUpdate"
                @click="$emit('edit-connection', conn)"
                class="p-1 rounded hover:bg-gray-200"
              >
                <UIcon name="heroicons-pencil-square" class="w-3.5 h-3.5 text-gray-400" />
              </button>
            </UTooltip>
            <UTooltip text="Remove connection">
              <button
                v-if="canUpdate"
                @click="$emit('delete-connection', conn)"
                class="p-1 rounded hover:bg-red-50"
              >
                <UIcon name="heroicons-trash" class="w-3.5 h-3.5 text-gray-400 hover:text-red-500" />
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
            class="border border-gray-200 rounded px-2 py-1 w-full max-w-xs h-7 text-xs focus:outline-none focus:border-blue-400"
          />
        </div>

        <!-- Tool list -->
        <ul v-if="getFilteredTools(conn.id).length > 0" class="divide-y divide-gray-100">
          <li
            v-for="tool in getFilteredTools(conn.id)"
            :key="tool.id"
            class="px-4 py-2 hover:bg-gray-50/50 transition-colors"
          >
            <div class="flex items-center gap-3">
              <UCheckbox
                v-if="canUpdate"
                color="blue"
                :model-value="tool.is_enabled"
                @update:model-value="(val: boolean) => toggleTool(conn.id, tool.id, val)"
              />
              <button
                type="button"
                class="flex items-center gap-1.5 text-start flex-shrink-0"
                @click="toggleExpand(tool.id)"
              >
                <UIcon
                  :name="expandedTools[tool.id] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
                  class="w-3 h-3 text-gray-400 flex-shrink-0 rtl-flip"
                />
                <code class="text-[13px] text-gray-800 font-medium whitespace-nowrap">{{ tool.name }}</code>
                <span v-if="!tool.is_enabled" class="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-400">off</span>
              </button>
              <span class="text-[11px] text-gray-400 truncate min-w-0">{{ tool.description }}</span>
            </div>

            <!-- Expanded -->
            <div v-if="expandedTools[tool.id]" class="mt-2 ms-9 space-y-2">
              <p v-if="tool.description" class="text-xs text-gray-500">{{ tool.description }}</p>
              <div v-if="tool.input_schema?.properties" class="text-xs">
                <div class="text-[10px] text-gray-400 uppercase font-medium mb-1">Parameters</div>
                <div class="grid gap-1">
                  <div
                    v-for="(prop, pname) in tool.input_schema.properties"
                    :key="pname"
                    class="flex items-baseline gap-2 text-xs"
                  >
                    <code class="text-[11px] text-blue-700 bg-blue-50 px-1 py-0.5 rounded">{{ pname }}</code>
                    <span class="text-gray-400">{{ prop.type || 'any' }}</span>
                    <span v-if="(tool.input_schema.required || []).includes(pname)" class="text-[9px] text-red-400">required</span>
                    <span v-if="prop.description" class="text-gray-500 truncate">— {{ prop.description }}</span>
                  </div>
                </div>
              </div>
              <details v-if="tool.input_schema" class="text-[10px] text-gray-400">
                <summary class="cursor-pointer hover:text-gray-600">Raw schema</summary>
                <pre class="mt-1 bg-gray-50 rounded p-2 text-[10px] font-mono text-gray-500 overflow-x-auto max-h-32 overflow-y-auto">{{ JSON.stringify(tool.input_schema, null, 2) }}</pre>
              </details>
            </div>
          </li>
        </ul>

        <!-- Empty tools -->
        <div v-else class="px-4 py-6 text-xs text-gray-400 text-center">
          {{ searchQuery ? 'No matching tools' : 'No tools discovered yet.' }}
          <button v-if="!searchQuery && canUpdate" @click="refreshTools(conn.id)" class="text-blue-500 hover:underline ms-1">Refresh</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{
  dsId: string
  connections: any[]
  canUpdate: boolean
}>()

defineEmits(['add-mcp', 'add-custom-api', 'edit-connection', 'delete-connection'])

const toast = useToast()

const loading = ref(false)
const refreshingConn = ref<string | null>(null)
const searchQuery = ref('')
const expandedTools = ref<Record<string, boolean>>({})

// Tools keyed by connection ID
const toolsByConnection = ref<Record<string, any[]>>({})

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
  loading.value = true
  try {
    await Promise.all(
      props.connections.map(conn => loadToolsForConnection(conn.id))
    )
  } finally {
    loading.value = false
  }
}

async function loadToolsForConnection(connectionId: string) {
  try {
    const response = await useMyFetch(`/connections/${connectionId}/tools`, { method: 'GET' })
    if (response.data.value) {
      toolsByConnection.value[connectionId] = response.data.value as any[]
    }
  } catch (e) {
    console.error(`Failed to load tools for connection ${connectionId}:`, e)
  }
}

async function refreshTools(connectionId: string) {
  refreshingConn.value = connectionId
  try {
    const response = await useMyFetch(`/connections/${connectionId}/refresh-tools`, { method: 'POST' })
    if (response.data.value) {
      toolsByConnection.value[connectionId] = response.data.value as any[]
      toast.add({ title: 'Tools refreshed', color: 'green' })
    }
  } catch (e) {
    toast.add({ title: 'Failed to refresh tools', color: 'red' })
  } finally {
    refreshingConn.value = null
  }
}

async function toggleTool(connectionId: string, toolId: string, enabled: boolean) {
  try {
    const response = await useMyFetch(`/connections/${connectionId}/tools/${toolId}`, {
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
