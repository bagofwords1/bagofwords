<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-clock" :label="cardData.title || toolExecution.tool_name" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded" />
    <div v-if="toolHeaderExpanded" class="mt-2 min-w-0 text-chat-2">
      <ScheduledTaskCard v-if="isSuccess" :scheduled-prompt="cardData" @click="taskId && emit('openScheduledTask', taskId)" />
      <div v-if="errorMessage" class="text-ink-muted break-words">{{ errorMessage }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)
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

const taskId = computed<string>(() => props.toolExecution?.result_json?.task_id || props.toolExecution?.arguments_json?.task_id || '')

// Build a scheduled-prompt-like object for the shared card.
const cardData = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  const args = props.toolExecution?.arguments_json || {}
  return {
    id: rj.task_id || args.task_id,
    title: rj.title || args.title || '',
    cron_schedule: rj.cron_schedule || args.cron_schedule || '',
    is_active: rj.is_active !== undefined ? rj.is_active : true,
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
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}
</style>
