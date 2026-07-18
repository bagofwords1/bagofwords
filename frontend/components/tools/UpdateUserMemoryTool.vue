<template>
  <div class="mt-1">
    <!-- Single-line, non-expandable status. Memory is private; we show only
         that it happened plus the agent's short label, never the content. -->
    <div class="flex items-center text-xs text-gray-500 dark:text-gray-400">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-bookmark" class="w-3 h-3 me-1.5 text-gray-400 dark:text-gray-500" />
        {{ label || $t('tools.updateUserMemory.updating') }}
      </span>
      <span v-else-if="isSuccess" class="text-gray-600 dark:text-gray-400 flex items-center">
        <Icon name="heroicons-bookmark" class="w-3 h-3 me-1.5 text-blue-500" />
        <span dir="auto" class="truncate max-w-[300px]">{{ label || $t('tools.updateUserMemory.updated') }}</span>
      </span>
      <span v-else class="text-gray-600 dark:text-gray-400 flex items-center">
        <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-red-500" />
        <span>{{ $t('tools.updateUserMemory.failed') }}</span>
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface Props {
  toolExecution: {
    tool_name: string
    arguments_json?: { content?: string; title?: string }
    result_json?: { success?: boolean; char_count?: number; error?: string }
    status: string
  }
  readonly?: boolean
}
const props = defineProps<Props>()

const status = computed(() => props.toolExecution.status)
const isSuccess = computed(() => status.value === 'success')
const label = computed(() => props.toolExecution.arguments_json?.title || '')
</script>
