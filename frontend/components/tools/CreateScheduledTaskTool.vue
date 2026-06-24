<template>
  <div class="mt-1">
    <!-- Running -->
    <div v-if="status === 'running'" class="flex items-center text-xs text-gray-500 dark:text-gray-400">
      <span class="tool-shimmer flex items-center">
        <Icon name="heroicons-clock" class="w-3 h-3 me-1.5 text-gray-400" />
        Scheduling task…
      </span>
    </div>

    <!-- Success: reuse the shared scheduled-task card; click opens the editor -->
    <Transition v-else-if="isSuccess" name="fade" appear>
      <ScheduledTaskCard
        :scheduled-prompt="cardData"
        @click="taskId && emit('openScheduledTask', taskId)"
      />
    </Transition>

    <!-- Error -->
    <div v-else class="text-xs text-gray-500 dark:text-gray-400">
      <div class="flex items-center text-gray-600 dark:text-gray-400">
        <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-red-500" />
        <span>Couldn't schedule task</span>
      </div>
      <div v-if="errorMessage" class="mt-1 text-[10px] text-red-500 bg-red-50/50 dark:bg-red-950 rounded px-2 py-1">
        {{ errorMessage }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()
const emit = defineEmits<{ (e: 'openScheduledTask', taskId: string): void }>()

const status = computed<string>(() => props.toolExecution?.status || '')

const isSuccess = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === true
})

const taskId = computed<string>(() => props.toolExecution?.result_json?.task_id || '')

// Build a scheduled-prompt-like object for the shared card.
const cardData = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  const args = props.toolExecution?.arguments_json || {}
  return {
    id: rj.task_id,
    cron_schedule: rj.cron_schedule || args.cron_schedule || '',
    is_active: true,
    prompt: { content: args.task_prompt || '' },
  }
})

const errorMessage = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  if (status.value === 'error') return rj.error || rj.message || ''
  if (status.value === 'success' && rj.success === false) return rj.error || ''
  return ''
})
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
</style>
