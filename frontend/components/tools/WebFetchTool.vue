<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-globe-alt" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">
      <template v-if="modelTitle">{{ modelTitle }}</template>
      <template v-else>{{ t('tools.webFetch.fetching') }}</template></template>
      <template v-else-if="isSuccess">{{ modelTitle || t('tools.webFetch.fetched') }}</template>
      <template v-else>{{ t('tools.webFetch.failed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'">
      <template v-if="modelTitle"></template>
      <template v-else><span v-if="displayUrl" dir="ltr" class="ms-1">{{ displayUrl }}</span></template></template>
      <template v-else-if="isSuccess"><span v-if="displayUrl && !modelTitle" dir="ltr" class="ms-1 truncate max-w-[320px] text-gray-600 dark:text-gray-400">{{ displayUrl }}</span><span v-if="statusCode" class="ms-1.5 text-chat-3 text-ink-muted shrink-0">{{ statusCode }}</span></template>
      <template v-else><span v-if="displayUrl" dir="ltr" class="ms-1 truncate max-w-[320px] text-gray-600 dark:text-gray-400">{{ displayUrl }}</span></template></div>

    <div v-if="!isSuccess && status !== 'running' && errorMessage" class="mt-1 ms-4 text-chat-2 text-gray-500 dark:text-gray-400">
      {{ errorMessage }}
    </div>
  
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

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const status = computed<string>(() => props.toolExecution?.status || '')

const modelTitle = computed<string>(() => {
  const t = props.toolExecution?.arguments_json?.title
  return typeof t === 'string' && t.trim() ? t.trim() : ''
})

const result = computed<any>(() => props.toolExecution?.result_json || {})

const isSuccess = computed(() => {
  return status.value === 'success' && result.value?.success === true
})

const displayUrl = computed<string>(() => {
  return result.value?.final_url
    || result.value?.url
    || props.toolExecution?.arguments_json?.url
    || ''
})

const statusCode = computed<number | null>(() => {
  const code = result.value?.status_code
  return typeof code === 'number' ? code : null
})

const errorMessage = computed<string>(() => {
  if (status.value === 'error') {
    return result.value?.error || result.value?.message || t('tools.webFetch.errorOccurred')
  }
  if (status.value === 'success' && result.value?.success === false) {
    return result.value?.error_message || t('tools.webFetch.errorOccurred')
  }
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
