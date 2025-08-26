<template>
  <div class="mb-2">
    <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCollapsed">
      <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1" />

      <!-- Status icon -->
      <Icon v-if="status === 'success'" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-red-500" />
      
      <!-- Action label with shimmer effect for running status -->
      <span v-if="status === 'running'" class="tool-shimmer">{{ actionLabel }}
      </span>
      <span v-else class="text-gray-700">{{ actionLabel }}</span>
      
      <!-- Row count if available -->
      <span v-if="rowCount" class="ml-2 text-gray-400">{{ rowCount }} rows</span>
      
      <!-- Execution time if > 2 seconds -->
      <span v-if="showDuration" class="ml-2 text-gray-400">{{ formatDuration }}</span>
    </div>
    
    <!-- Collapsible content -->
    <Transition name="fade">
      <div v-if="!isCollapsed" class="mt-1 ml-4">
        <!-- Minimalistic code display -->
        <div v-if="codeContent" class="text-xs mb-2">
          <div class="bg-gray-50 rounded px-4 py-3 font-mono text-xs max-h-42 overflow-y-auto">
            <pre class="text-gray-800 whitespace-pre-wrap">{{ codeContent }}</pre>
          </div>
        </div>

        <!-- Loading state -->
        <div v-else-if="status === 'running'" class="text-xs text-gray-500 italic">
        </div>
        
        <!-- Result summary fallback -->
        <div v-else-if="resultSummary" class="text-xs text-gray-600">
          {{ resultSummary }}
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
    arguments_json?: {
      prompt?: string
    }
    result_json?: {
      success?: boolean
      summary?: string
      code?: string
      execution_log?: string
      widget_data?: {
        rows?: Array<Record<string, any>>
        info?: {
          total_rows?: number
          total_columns?: number
        }
      }
      data_preview?: {
        rows?: Array<Record<string, any>>
      }
      stats?: {
        total_rows?: number
      }
    }
    status: string
    result_summary?: string
    duration_ms?: number
    created_widget_id?: string
    created_step_id?: string
  }
}

const props = defineProps<Props>()

const isCollapsed = ref(true) // Start collapsed

const status = computed(() => props.toolExecution.status)
const resultSummary = computed(() => props.toolExecution.result_summary)

const codeContent = computed(() => {
  return props.toolExecution.result_json?.code || ''
})

const executionLog = computed(() => {
  return props.toolExecution.result_json?.execution_log || ''
})

const dataPreview = computed(() => {
  // Try multiple possible paths for data preview
  return props.toolExecution.result_json?.data_preview?.rows || 
         props.toolExecution.result_json?.widget_data?.rows || []
})

const rowCount = computed(() => {
  // Try multiple possible paths for row count
  const totalRows = props.toolExecution.result_json?.stats?.total_rows ||
                   props.toolExecution.result_json?.widget_data?.info?.total_rows
  if (totalRows !== undefined) {
    return `${totalRows.toLocaleString()}`
  }
  return null
})

// Cursor-like action label
const actionLabel = computed(() => {
  if (status.value === 'running') return 'Generating code'
  if (status.value === 'success') return `Created data`
  if (status.value === 'error') return 'Code generation failed'
  return 'Generate code'
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

function formatCellValue(value: any): string {
  if (value === null || value === undefined) {
    return '—'
  }
  if (typeof value === 'number') {
    return value.toLocaleString()
  }
  if (typeof value === 'string' && value.length > 50) {
    return value.substring(0, 50) + '...'
  }
  return String(value)
}

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

