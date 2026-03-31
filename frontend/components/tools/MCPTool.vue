<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center gap-1">
          <Icon name="heroicons-server-stack" class="w-3 h-3 mr-1 text-gray-400" />
          <span>{{ runningLabel }}</span>
        </span>
        <span v-else class="text-gray-600 flex items-center gap-1">
          <Icon name="heroicons-server-stack" class="w-3 h-3 mr-1 text-gray-400" />
          <span>{{ doneLabel }}</span>
          <span v-if="duration" class="text-gray-400 ml-1">{{ duration }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ml-1 text-gray-400"
          />
        </span>
      </div>
    </Transition>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded && status !== 'running'" class="mt-2 space-y-1.5">
        <!-- Result preview -->
        <div v-if="preview" class="group">
          <div
            class="flex items-center text-[11px] text-gray-500 cursor-pointer hover:text-gray-600 mb-0.5"
            @click="showPreview = !showPreview"
          >
            <Icon
              :name="showPreview ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
              class="w-2.5 h-2.5 mr-1 text-gray-400"
            />
            <span>Output</span>
          </div>
          <div v-if="showPreview" class="max-h-28 overflow-auto rounded bg-gray-50 border border-gray-100">
            <pre class="text-[10px] leading-tight text-gray-600 p-2 m-0 whitespace-pre-wrap break-words font-mono">{{ preview }}</pre>
          </div>
        </div>

        <!-- Error message -->
        <div v-if="errorMessage" class="text-[10px] text-red-500 bg-red-50/50 rounded px-2 py-1">
          {{ errorMessage }}
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

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
}>()

const isExpanded = ref(false)
const showPreview = ref(true)

const status = computed(() => props.toolExecution?.status || '')
const toolName = computed(() => props.toolExecution?.tool_name || '')
const args = computed(() => props.toolExecution?.arguments_json || {})
const resultJson = computed(() => props.toolExecution?.result_json || {})

const duration = computed(() => {
  const ms = props.toolExecution?.duration_ms
  if (!ms) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
})

const runningLabel = computed(() => {
  if (toolName.value === 'search_mcps') return 'Searching MCP tools…'
  if (toolName.value === 'execute_mcp') {
    const connName = resultJson.value.connection_name
    const label = connName || args.value.tool_name || 'MCP tool'
    return `Calling ${label}…`
  }
  if (toolName.value === 'write_csv') return 'Writing CSV…'
  return 'Running MCP tool…'
})

const doneLabel = computed(() => {
  if (toolName.value === 'search_mcps') {
    const count = resultJson.value.total_count ?? resultJson.value.tools?.length ?? 0
    return `Found ${count} tool(s)`
  }
  if (toolName.value === 'execute_mcp') {
    const connName = resultJson.value.connection_name || args.value.tool_name || 'MCP tool'
    if (resultJson.value.file_id) return `${connName} → CSV`
    if (resultJson.value.success === false) return `${connName} (failed)`
    return `${connName}`
  }
  if (toolName.value === 'write_csv') {
    const rows = resultJson.value.row_count
    return rows ? `Wrote ${rows} rows to CSV` : 'CSV written'
  }
  return 'MCP tool'
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
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}

@keyframes shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: 100% 0; }
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
