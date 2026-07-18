<template>
  <div class="mt-1">
    <div class="mb-2 flex items-center text-xs text-gray-700 dark:text-gray-300">
      <Icon
        :name="status === 'running' ? 'heroicons-arrow-path' : 'heroicons-arrows-right-left'"
        class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500 flex-shrink-0"
        :class="{ 'animate-spin': status === 'running' }"
      />
      <span class="align-middle">{{ label }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed<string>(() => props.toolExecution?.status || '')

const label = computed<string>(() => {
  if (status.value === 'running') return 'Choosing model…'
  const rj: any = props.toolExecution?.result_json || {}
  const name = rj.model_name || rj.model
  if (rj.routed && name) return `Routed to ${name}`
  // Not routed (kept current model, or invalid target) — fall back to the summary.
  return props.toolExecution?.result_summary || 'Model routing'
})
</script>
