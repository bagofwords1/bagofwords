<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center flex-wrap gap-1">
          <Icon name="heroicons-command-line" class="w-3 h-3 mr-1 text-gray-400" />
          <span>Inspecting</span>
          <template v-if="groupedTables.length">
            <template v-for="(group, gidx) in groupedTables" :key="gidx">
              <span v-if="gidx > 0" class="text-gray-300">|</span>
              <DataSourceIcon :type="group.type" class="h-3" />
              <span>{{ group.names.join(', ') }}</span>
            </template>
          </template>
          <span v-else>data…</span>
        </span>
        <span v-else class="text-gray-600 flex items-center flex-wrap gap-1">
          <Icon name="heroicons-command-line" class="w-3 h-3 mr-1 text-gray-400" />
          <span>Inspected</span>
          <template v-if="groupedTables.length">
            <template v-for="(group, gidx) in groupedTables" :key="gidx">
              <span v-if="gidx > 0" class="text-gray-300">|</span>
              <DataSourceIcon :type="group.type" class="h-3" />
              <span>{{ group.names.join(', ') }}</span>
            </template>
          </template>
          <span v-else>data</span>
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
        <!-- Code section -->
        <div v-if="code" class="group">
          <div 
            class="flex items-center text-[11px] text-gray-500 cursor-pointer hover:text-gray-600 mb-0.5"
            @click="toggleCode"
          >
            <Icon 
              :name="showCode ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" 
              class="w-2.5 h-2.5 mr-1 text-gray-400" 
            />
            <span>Code</span>
          </div>
          <div v-if="showCode" class="max-h-24 overflow-auto rounded bg-gray-50 border border-gray-100">
            <pre class="text-[10px] leading-tight text-gray-600 p-2 m-0 whitespace-pre-wrap break-words">{{ code }}</pre>
          </div>
        </div>

        <!-- Output section -->
        <div v-if="output" class="group">
          <div 
            class="flex items-center text-[11px] text-gray-500 cursor-pointer hover:text-gray-600 mb-0.5"
            @click="toggleOutput"
          >
            <Icon 
              :name="showOutput ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" 
              class="w-2.5 h-2.5 mr-1 text-gray-400" 
            />
            <span>Output</span>
          </div>
          <div v-if="showOutput" class="max-h-28 overflow-auto rounded bg-gray-50 border border-gray-100">
            <pre class="text-[10px] leading-tight text-gray-600 p-2 m-0 whitespace-pre-wrap break-words font-mono">{{ output }}</pre>
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
import DataSourceIcon from '~/components/DataSourceIcon.vue'

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

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const isExpanded = ref(false)
const showCode = ref(false)
const showOutput = ref(true)

const status = computed<string>(() => props.toolExecution?.status || '')

const duration = computed<string>(() => {
  // Prefer execution_duration_ms from result (actual code execution time) over total tool duration
  const rj = props.toolExecution?.result_json || {}
  const ms = rj.execution_duration_ms ?? props.toolExecution?.duration_ms
  if (!ms) return ''
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
})

const code = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.code || ''
})

const output = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.execution_log || rj.details || ''
})

const errorMessage = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.error_message || ''
})

// Group resolved tables by connection type for display
const groupedTables = computed<Array<{ type: string; names: string[] }>>(() => {
  const rj = props.toolExecution?.result_json || {}
  const tables = Array.isArray(rj.resolved_tables) ? rj.resolved_tables : []
  if (!tables.length) return []

  const groups: Record<string, string[]> = {}
  for (const t of tables) {
    const ctype = t.connection_type || 'resource'
    if (!groups[ctype]) groups[ctype] = []
    if (t.name) groups[ctype].push(t.name)
  }

  return Object.entries(groups).map(([type, names]) => ({ type, names }))
})

function toggleExpanded() {
  if (status.value !== 'running') {
    isExpanded.value = !isExpanded.value
  }
}

function toggleCode() {
  showCode.value = !showCode.value
}

function toggleOutput() {
  showOutput.value = !showOutput.value
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
