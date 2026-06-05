<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-clock" class="w-3 h-3 me-1.5 text-gray-400" />
          Scheduling task…
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 flex items-center">
          <Icon name="heroicons-clock" class="w-3 h-3 me-1.5 text-blue-500" />
          <span dir="auto" class="truncate max-w-[300px]">Scheduled — {{ scheduleLabel }}</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 shrink-0 rtl-flip"
          />
        </span>
        <span v-else class="text-gray-600 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-red-500" />
          <span>Couldn't schedule task</span>
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 ms-1 text-gray-400 rtl-flip"
          />
        </span>
      </div>
    </Transition>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded && status !== 'running'" class="mt-2 space-y-2">
        <div class="border border-gray-150 rounded-md overflow-hidden">
          <div class="px-3 py-1.5 bg-gray-50 border-b border-gray-150 flex items-center gap-2">
            <span class="text-[10px] text-gray-600 font-medium">Schedule</span>
            <span dir="auto" class="text-[11px] text-gray-800 truncate">{{ scheduleLabel }}</span>
            <span v-if="cronSchedule" class="text-[10px] text-gray-400 font-mono">{{ cronSchedule }}</span>
          </div>
          <div v-if="taskPrompt" class="px-3 py-2 bg-white">
            <pre dir="auto" class="text-[11px] text-gray-700 whitespace-pre-wrap font-sans m-0">{{ truncatedPrompt }}</pre>
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

const scheduleLabel = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  const rj = props.toolExecution?.result_json || {}
  return rj.schedule_description || args.schedule_description || 'scheduled'
})

const cronSchedule = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  const rj = props.toolExecution?.result_json || {}
  return rj.cron_schedule || args.cron_schedule || ''
})

const taskPrompt = computed(() => {
  const args = props.toolExecution?.arguments_json || {}
  return args.task_prompt || ''
})

const truncatedPrompt = computed(() => {
  const p = taskPrompt.value
  if (!p) return ''
  return p.length > 1000 ? p.substring(0, 1000) + '…' : p
})

const errorMessage = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  if (status.value === 'error') {
    return rj.error || rj.message || ''
  }
  if (status.value === 'success' && rj.success === false) {
    return rj.error || ''
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
  max-height: 500px;
}
</style>
