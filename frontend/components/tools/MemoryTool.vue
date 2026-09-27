<template>
  <div class="mt-1">
    <!-- Single-line, non-expandable status for create_memory / edit_memory /
         search_memory (and legacy update_user_memory). Memory is private: we
         show only that it happened plus the agent's short title — never entry
         text, handles, tags or search results. Details live in the profile. -->
    <div class="flex items-center text-xs text-gray-500 dark:text-gray-400" data-testid="memory-tool">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-bookmark" class="w-3 h-3 me-1.5 text-gray-400 dark:text-gray-500" />
        <span dir="auto" class="truncate max-w-[300px]">{{ label || runningFallback }}</span>
      </span>
      <span v-else-if="isSuccess" class="text-gray-600 dark:text-gray-400 flex items-center">
        <Icon name="heroicons-bookmark" class="w-3 h-3 me-1.5 text-blue-500" />
        <span dir="auto" class="truncate max-w-[300px]">{{ label || doneFallback }}</span>
      </span>
      <span v-else class="text-gray-600 dark:text-gray-400 flex items-center">
        <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-red-500" />
        <span>{{ $t('tools.memory.failed') }}</span>
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface Props {
  toolExecution: {
    tool_name: string
    arguments_json?: { title?: string }
    result_json?: { success?: boolean }
    status: string
  }
  readonly?: boolean
}
const props = defineProps<Props>()
const { t } = useI18n()

const status = computed(() => props.toolExecution.status)
const isSearch = computed(() => props.toolExecution.tool_name === 'search_memory')
// A tool that ran but refused the write (validation, dedupe refusal, …)
// ends with success=false in its result — that is a failure for this line.
const isSuccess = computed(() =>
  status.value === 'success' && props.toolExecution.result_json?.success !== false
)
const label = computed(() => props.toolExecution.arguments_json?.title || '')
const runningFallback = computed(() => isSearch.value ? t('tools.memory.checking') : t('tools.memory.updating'))
const doneFallback = computed(() => isSearch.value ? t('tools.memory.checked') : t('tools.memory.updated'))
</script>
