<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-clock" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.cancelWait.cancelling') }}</template>
      <template v-else-if="cancelled.length">{{ cancelled.length === 1 ? t('tools.cancelWait.cancelledSingular', { count: cancelled.length }) : t('tools.cancelWait.cancelledPlural', { count: cancelled.length }) }}</template>
      <template v-else>{{ t('tools.cancelWait.nothingPending') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <p v-if="message" class="mb-2 text-chat-3 text-ink-muted break-words">{{ message }}</p>

    <ul v-if="cancelled.length" class="text-chat-2 text-gray-600 dark:text-gray-400 ms-1 space-y-0.5 leading-snug">
      <li v-for="c in cancelled" :key="c.job_id" class="flex items-center py-0.5 px-1 rounded">
        <Icon name="heroicons-clock" class="w-3 h-3 me-1 text-ink-muted flex-shrink-0" />
        <span class="truncate text-chat-2" :title="c.reason || c.job_id">{{ c.reason || c.job_id }}</span>
      </li>
    </ul>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'

const { t } = useResponseI18n()

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
.tool-call-details { overflow-wrap: anywhere; }
</style>
