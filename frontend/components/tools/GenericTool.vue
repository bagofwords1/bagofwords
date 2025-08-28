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
      <div v-if="!isCollapsed" class="mt-1 ml-4 space-y-2">
        <!-- Input arguments -->
        <div v-if="inputSummary">
          <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Input</div>
          <div class="text-xs text-gray-700">{{ inputSummary }}</div>
        </div>

        <!-- Status -->
        <div class="text-xs">
          <span v-if="status === 'running'" class="tool-shimmer">Running...</span>
          <span v-else-if="status === 'success'" class="text-green-600">✓ Success</span>
          <span v-else-if="status === 'error'" class="text-red-600">✗ {{ statusReason || 'Failed' }}</span>
          <span v-else>{{ status }}</span>
        </div>

        <!-- Output -->
        <div v-if="outputSummary">
          <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Output</div>
          <div class="text-xs text-gray-700">{{ outputSummary }}</div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

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

const status = computed(() => props.toolExecution.status)
const statusReason = computed(() => props.toolExecution.status_reason)

const toolTitle = computed(() => {
  const name = props.toolExecution.tool_name
  const action = props.toolExecution.tool_action
  return action ? `${name} → ${action}` : name
})

const inputSummary = computed(() => {
  const args = props.toolExecution.arguments_json
  if (!args) return ''
  
  // Create a concise summary of inputs
  const keys = Object.keys(args).slice(0, 3) // Show first 3 args
  if (keys.length === 0) return ''
  
  const summary = keys.map(key => {
    let value = args[key]
    if (typeof value === 'string' && value.length > 50) {
      value = value.substring(0, 50) + '...'
    } else if (typeof value === 'object') {
      value = JSON.stringify(value).substring(0, 50) + '...'
    }
    return `${key}: ${value}`
  }).join(', ')
  
  return keys.length < Object.keys(args).length ? summary + '...' : summary
})

const outputSummary = computed(() => {
  if (props.toolExecution.result_summary) {
    return props.toolExecution.result_summary
  }
  
  const result = props.toolExecution.result_json
  if (!result) return ''
  
  // Try to extract meaningful output
  if (result.success !== undefined) {
    return result.success ? 'Operation completed successfully' : 'Operation failed'
  }
  
  if (typeof result === 'object') {
    const keys = Object.keys(result).slice(0, 2)
    if (keys.length > 0) {
      return keys.map(key => `${key}: ${String(result[key]).substring(0, 30)}`).join(', ')
    }
  }
  
  return String(result).substring(0, 100)
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