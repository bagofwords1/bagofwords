<template>
  <div class="mb-2">
    <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCollapsed">
      <Icon :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-3 h-3 mr-1" />

      <!-- Status icon -->
      <Icon v-if="status === 'success'" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-red-500" />
      
      <!-- Action label with shimmer effect for running status -->
      <span v-if="status === 'running'" class="tool-shimmer">
        {{ actionLabel }}
      </span>
      <span v-else class="text-gray-700">{{ actionLabel }}</span>
      
      <!-- Execution time if > 2 seconds -->
      <span v-if="showDuration" class="ml-2 text-gray-400">{{ formatDuration }}</span>
    </div>
    
    <!-- Collapsible content -->
    <Transition name="fade">
      <div v-if="!isCollapsed" class="mt-1 ml-4">
        <!-- Minimalistic data model table -->
        <div v-if="dataModelColumns.length > 0" class="text-xs mt-2">
          <table class="w-full text-xs mt-2">
            <tbody>
              <tr v-for="column in dataModelColumns" :key="column.generated_column_name" 
                  class="border-b border-gray-100">
                <td class="font-mono text-gray-800 py-1 pr-4 align-top">
                  {{ column.generated_column_name }}
                </td>
                <td class="text-gray-500 py-1 leading-tight">
                  {{ column.description }}
                  
                  <span v-if="column.source" class="text-gray-400 text-xs">
                    <Icon name="heroicons-circle-stack" class="w-3 h-3 ml-1 text-gray-400" />
                    {{ column.source }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        
        <!-- Loading state -->
        <div v-else-if="status === 'running'" class="text-xs text-gray-500 italic">
        </div>
        
        <!-- Result summary fallback -->
        <div v-else-if="resultSummary" class="text-xs text-gray-600">
          {{ resultSummary }}
        </div>
        
        <!-- Error message -->
        <div v-if="status === 'error'" class="text-xs text-red-500 mt-1">
          {{ resultSummary || 'Failed to create data model' }}
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
      widget_title?: string
      prompt?: string
    }
    result_json?: {
      data_model?: {
        columns?: Array<{
          generated_column_name: string
          description: string
          source?: string
        }>
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

const widgetTitle = computed(() => {
  return props.toolExecution.arguments_json?.widget_title || props.toolExecution.arguments_json?.prompt || 'Data Model'
})

const status = computed(() => props.toolExecution.status)
const resultSummary = computed(() => props.toolExecution.result_summary)

const dataModelColumns = computed(() => {
  return props.toolExecution.result_json?.data_model?.columns || []
})

const actionLabel = computed(() => {
  if (status.value === 'running') return 'Creating data model'
  if (status.value === 'success') return `Created data model`
  if (status.value === 'error') return 'Failed to create data model'
  return 'Create data model'
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
