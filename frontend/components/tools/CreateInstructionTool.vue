<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-light-bulb" class="w-3 h-3 mr-1.5 text-gray-400" />
          Creating instruction...
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 flex items-center">
          <Icon name="heroicons-check-circle" class="w-3 h-3 mr-1.5 text-green-500" />
          <span>Created instruction</span>
          <span v-if="category" class="ml-1.5 px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded text-[10px]">{{ category }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ml-1 text-gray-400"
          />
        </span>
        <span v-else-if="isRejected" class="text-gray-600 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 mr-1.5 text-orange-500" />
          <span>Instruction rejected</span>
          <span v-if="rejectedReason" class="ml-1.5 text-orange-600 text-[10px]">({{ rejectedReason }})</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ml-1 text-gray-400"
          />
        </span>
        <span v-else class="text-gray-600 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 mr-1.5 text-red-500" />
          <span>Failed to create instruction</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ml-1 text-gray-400"
          />
        </span>
      </div>
    </Transition>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded && status !== 'running'" class="mt-2 space-y-2">
        <!-- Instruction text -->
        <div v-if="instructionText" class="bg-gray-50 border border-gray-100 rounded-md p-2.5">
          <MDC :value="instructionText" class="text-[11px] text-gray-700 leading-relaxed prose prose-sm max-w-none" />
        </div>

        <!-- Metadata row -->
        <div class="flex flex-wrap items-center gap-2 text-[10px]">
          <!-- Confidence -->
          <div v-if="confidence" class="flex items-center gap-1">
            <span class="text-gray-500">Confidence:</span>
            <span
              class="font-medium"
              :class="confidence >= 0.9 ? 'text-green-600' : confidence >= 0.7 ? 'text-yellow-600' : 'text-red-600'"
            >
              {{ Math.round(confidence * 100) }}%
            </span>
          </div>

          <!-- Load mode -->
          <div v-if="loadMode" class="flex items-center gap-1">
            <span class="text-gray-500">Load:</span>
            <span class="px-1.5 py-0.5 rounded text-[9px] font-medium"
              :class="loadMode === 'always' ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-600'"
            >
              {{ loadMode }}
            </span>
          </div>

          <!-- Tables scoped -->
          <div v-if="tableCount > 0" class="flex items-center gap-1">
            <Icon name="heroicons-table-cells" class="w-3 h-3 text-gray-400" />
            <span class="text-gray-600">{{ tableCount }} table{{ tableCount > 1 ? 's' : '' }}</span>
          </div>
        </div>

        <!-- Evidence (if available) -->
        <div v-if="evidence" class="text-[10px] text-gray-500 italic">
          <span class="font-medium">Evidence:</span> {{ evidence }}
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

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const isExpanded = ref(false)

const status = computed<string>(() => props.toolExecution?.status || '')

const isSuccess = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === true
})

const isRejected = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === false && rj.rejected_reason
})

// Extract from arguments_json (input)
const instructionText = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.text || ''
})

const category = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.category || ''
})

const confidence = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.confidence
})

const loadMode = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.load_mode || 'intelligent'
})

const evidence = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.evidence || ''
})

const tableCount = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  const tableNames = args.table_names || []
  return Array.isArray(tableNames) ? tableNames.length : 0
})

// Extract from result_json (output)
const rejectedReason = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return rj.rejected_reason || ''
})

const errorMessage = computed(() => {
  if (status.value === 'error') {
    const rj = props.toolExecution?.result_json || {}
    return rj.error || rj.message || 'An error occurred'
  }
  if (isRejected.value) {
    const rj = props.toolExecution?.result_json || {}
    return rj.message || ''
  }
  return ''
})

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
