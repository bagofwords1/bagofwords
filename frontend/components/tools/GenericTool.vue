<template>
  <div class="mb-2">
    <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCollapsed">
      <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1" />

      <!-- Status icon -->
      <Icon v-if="status === 'success'" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-red-500" />
      
      <!-- Tool title with shimmer effect for running status -->
      <span v-if="status === 'running'" class="tool-shimmer">{{ toolTitle }}
      </span>
      <span v-else class="text-gray-700">{{ toolTitle }}</span>
      
      <!-- Execution time if > 2 seconds -->
      <span v-if="showDuration" class="ml-2 text-gray-400">{{ formatDuration }}</span>
    </div>
    
    <!-- Collapsible content -->
    <Transition name="fade">
      <div v-if="!isCollapsed" class="mt-2 ml-4 space-y-3">
        <!-- Input arguments - Full Display -->
        <div v-if="hasInput">
          <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1.5">Input</div>
          <div class="border rounded-md overflow-hidden">
            <!-- Structured Input Fields -->
            <div class="divide-y divide-gray-100">
              <div v-for="(value, key) in inputArgs" :key="key" class="px-3 py-2">
                <div class="flex items-start gap-2">
                  <span class="text-[11px] font-medium text-gray-500 min-w-[80px] pt-0.5">{{ key }}</span>
                  <div class="flex-1 min-w-0">
                    <!-- String values -->
                    <template v-if="typeof value === 'string'">
                      <div v-if="value.length > 150" class="space-y-1">
                        <div class="text-xs text-gray-800 whitespace-pre-wrap break-words">
                          {{ showFullInput[key] ? value : value.slice(0, 150) + '...' }}
                        </div>
                        <button 
                          class="text-[10px] text-blue-600 hover:text-blue-800"
                          @click.stop="toggleFullInput(key)"
                        >
                          {{ showFullInput[key] ? 'Show less' : `Show all (${value.length} chars)` }}
                        </button>
                      </div>
                      <div v-else class="text-xs text-gray-800 whitespace-pre-wrap break-words">{{ value }}</div>
                    </template>
                    <!-- Number/Boolean values -->
                    <template v-else-if="typeof value === 'number' || typeof value === 'boolean'">
                      <span class="text-xs text-gray-800 font-mono">{{ value }}</span>
                    </template>
                    <!-- Array values -->
                    <template v-else-if="Array.isArray(value)">
                      <div class="space-y-1">
                        <div class="flex flex-wrap gap-1">
                          <span 
                            v-for="(item, idx) in (showFullInput[key] ? value : value.slice(0, 5))" 
                            :key="idx"
                            class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-gray-100 text-gray-700"
                          >
                            {{ typeof item === 'object' ? JSON.stringify(item) : item }}
                          </span>
                          <span v-if="!showFullInput[key] && value.length > 5" class="text-[10px] text-gray-400">
                            +{{ value.length - 5 }} more
                          </span>
                        </div>
                        <button 
                          v-if="value.length > 5"
                          class="text-[10px] text-blue-600 hover:text-blue-800"
                          @click.stop="toggleFullInput(key)"
                        >
                          {{ showFullInput[key] ? 'Show less' : `Show all (${value.length} items)` }}
                        </button>
                      </div>
                    </template>
                    <!-- Object values -->
                    <template v-else-if="typeof value === 'object' && value !== null">
                      <div class="space-y-1">
                        <pre class="text-[11px] text-gray-800 whitespace-pre-wrap break-words bg-gray-50 rounded px-2 py-1 overflow-x-auto max-h-32" :class="{ 'max-h-none': showFullInput[key] }">{{ formatObject(value, showFullInput[key]) }}</pre>
                        <button 
                          v-if="JSON.stringify(value).length > 200"
                          class="text-[10px] text-blue-600 hover:text-blue-800"
                          @click.stop="toggleFullInput(key)"
                        >
                          {{ showFullInput[key] ? 'Collapse' : 'Expand' }}
                        </button>
                      </div>
                    </template>
                    <!-- Null/undefined -->
                    <template v-else>
                      <span class="text-xs text-gray-400 italic">null</span>
                    </template>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Status -->
        <div class="text-xs">
          <span v-if="status === 'running'" class="tool-shimmer">Running...</span>
          <span v-else-if="status === 'success'" class="text-green-600 font-medium">✓ Success</span>
          <span v-else-if="status === 'error'" class="text-red-600 font-medium">✗ {{ statusReason || 'Failed' }}</span>
          <span v-else>{{ status }}</span>
        </div>

        <!-- Output - Full Display -->
        <div v-if="hasOutput">
          <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1.5">Output</div>
          <div class="border rounded-md overflow-hidden">
            <!-- Result Summary (if available) -->
            <div v-if="toolExecution.result_summary" class="px-3 py-2 bg-green-50 border-b border-green-100">
              <div class="text-xs text-green-800">{{ toolExecution.result_summary }}</div>
            </div>
            
            <!-- Full Result Data -->
            <div v-if="outputData" class="px-3 py-2">
              <!-- String output -->
              <template v-if="typeof outputData === 'string'">
                <div class="text-xs text-gray-800 whitespace-pre-wrap break-words">
                  {{ showFullOutput ? outputData : truncateString(outputData, 300) }}
                </div>
                <button 
                  v-if="outputData.length > 300"
                  class="text-[10px] text-blue-600 hover:text-blue-800 mt-1"
                  @click.stop="showFullOutput = !showFullOutput"
                >
                  {{ showFullOutput ? 'Show less' : `Show all (${outputData.length} chars)` }}
                </button>
              </template>
              <!-- Object output -->
              <template v-else-if="typeof outputData === 'object'">
                <div class="space-y-2">
                  <!-- Show key fields prominently -->
                  <div v-if="outputData.summary" class="text-xs text-gray-800">
                    <span class="text-gray-500">Summary:</span> {{ outputData.summary }}
                  </div>
                  <div v-if="outputData.error" class="text-xs text-red-700 bg-red-50 rounded px-2 py-1">
                    <span class="font-medium">Error:</span> 
                    {{ typeof outputData.error === 'string' ? outputData.error : outputData.error.message || JSON.stringify(outputData.error) }}
                  </div>
                  
                  <!-- Full JSON (collapsible) -->
                  <div>
                    <button 
                      class="text-[10px] text-blue-600 hover:text-blue-800 flex items-center gap-1"
                      @click.stop="showFullOutput = !showFullOutput"
                    >
                      <Icon :name="showFullOutput ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3" />
                      {{ showFullOutput ? 'Hide full output' : 'Show full output' }}
                    </button>
                    <Transition name="fade">
                      <pre v-if="showFullOutput" class="mt-1 text-[11px] text-gray-800 whitespace-pre-wrap break-words bg-gray-50 rounded px-2 py-1.5 overflow-x-auto max-h-64 overflow-y-auto">{{ JSON.stringify(outputData, null, 2) }}</pre>
                    </Transition>
                  </div>
                </div>
              </template>
              <!-- Other types -->
              <template v-else>
                <div class="text-xs text-gray-800">{{ outputData }}</div>
              </template>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, reactive } from 'vue'

interface Props {
  toolExecution: {
    id: string
    tool_name: string
    tool_action?: string
    arguments_json?: any
    result_json?: any
    status: string
    status_reason?: string
    result_summary?: string
    duration_ms?: number
  }
}

const props = defineProps<Props>()

const isCollapsed = ref(true) // Start collapsed
const showFullInput = reactive<Record<string, boolean>>({})
const showFullOutput = ref(false)

const status = computed(() => props.toolExecution.status)
const statusReason = computed(() => props.toolExecution.status_reason)

const toolTitle = computed(() => {
  const name = props.toolExecution.tool_name
  const action = props.toolExecution.tool_action
  return action ? `${name} → ${action}` : name
})

// Input handling
const inputArgs = computed(() => {
  return props.toolExecution.arguments_json || {}
})

const hasInput = computed(() => {
  const args = props.toolExecution.arguments_json
  return args && Object.keys(args).length > 0
})

// Output handling
const outputData = computed(() => {
  return props.toolExecution.result_json
})

const hasOutput = computed(() => {
  return props.toolExecution.result_json !== undefined && props.toolExecution.result_json !== null
})

// Show duration if > 2 seconds
const showDuration = computed(() => {
  return props.toolExecution.duration_ms && props.toolExecution.duration_ms > 2000
})

const formatDuration = computed(() => {
  if (!props.toolExecution.duration_ms) return ''
  const seconds = (props.toolExecution.duration_ms / 1000).toFixed(1)
  return `${seconds}s`
})

function toggleCollapsed() {
  isCollapsed.value = !isCollapsed.value
}

function toggleFullInput(key: string) {
  showFullInput[key] = !showFullInput[key]
}

function formatObject(obj: any, full: boolean = false): string {
  try {
    const str = JSON.stringify(obj, null, 2)
    if (!full && str.length > 200) {
      return str.slice(0, 200) + '\n...'
    }
    return str
  } catch {
    return String(obj)
  }
}

function truncateString(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str
  return str.slice(0, maxLen) + '...'
}

// Auto-collapse when execution finishes
watch(() => status.value, (newStatus, oldStatus) => {
  // Auto-expand when execution starts
  if (newStatus === 'running') {
    isCollapsed.value = false
  }
  // Auto-collapse when execution finishes
  else if (oldStatus === 'running' && (newStatus === 'success' || newStatus === 'error')) {
    // Delay collapse to show result briefly
    setTimeout(() => {
      isCollapsed.value = true
    }, 2000) // 2 second delay
  }
}, { immediate: true })
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

@keyframes shimmer {
	0% { background-position: -100% 0; }
	100% { background-position: 100% 0; }
}

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
</style>