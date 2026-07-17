<template>
  <div class="mt-1">
    <div class="mb-1 flex items-center text-xs text-gray-500 dark:text-gray-400">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-bell-slash" class="w-3 h-3 me-1 text-gray-400" />
        {{ t('tools.cancelWait.cancelling') }}
      </span>
      <span v-else-if="cancelled.length" class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-bell-slash" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">{{ cancelled.length === 1 ? t('tools.cancelWait.cancelledSingular', { count: cancelled.length }) : t('tools.cancelWait.cancelledPlural', { count: cancelled.length }) }}</span>
      </span>
      <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-bell-slash" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">{{ message || t('tools.cancelWait.nothingPending') }}</span>
      </span>
    </div>

    <ul v-if="cancelled.length" class="text-xs text-gray-600 dark:text-gray-400 ms-1 space-y-0.5 leading-snug">
      <li v-for="c in cancelled" :key="c.job_id" class="flex items-center py-0.5 px-1 rounded">
        <Icon name="heroicons-clock" class="w-3 h-3 me-1 text-gray-400 flex-shrink-0" />
        <span class="truncate text-[11px]" :title="c.reason || c.job_id">{{ c.reason || c.job_id }}</span>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const cancelled = computed<any[]>(() => Array.isArray(result.value?.cancelled) ? result.value.cancelled : [])
const message = computed<string>(() => result.value?.message || '')
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
</style>
