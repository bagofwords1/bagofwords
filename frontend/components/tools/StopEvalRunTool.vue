<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-clipboard-document-check" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.stopEvalRun.stopping') }}</template>
      <template v-else-if="success">{{ t('tools.stopEvalRun.stopped') }}</template>
      <template v-else>{{ t('tools.stopEvalRun.failed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'"></template>
      <template v-else-if="success"><span v-if="total" class="ms-1.5 text-chat-3 text-gray-500 dark:text-gray-400">{{ passed }}/{{ total }} {{ t('tools.stopEvalRun.passedAtStop') }}</span></template>
      <template v-else></template></div>
      <p v-if="message" class="mb-2 text-chat-3 text-ink-muted break-words">{{ message }}</p>
      
    <div v-if="runId" class="text-chat-2 text-ink-muted ms-1">
      <NuxtLink :to="`/evals/runs/${runId}`" class="hover:text-blue-600 inline-flex items-center gap-0.5">
        <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
        {{ t('tools.stopEvalRun.openRun') }}
      </NuxtLink>
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

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const success = computed<boolean>(() => !!result.value?.success)
const runId = computed<string>(() => result.value?.run_id || '')
const total = computed<number>(() => result.value?.total || 0)
const passed = computed<number>(() => result.value?.passed || 0)
const message = computed<string>(() => result.value?.message || '')
</script>

<style scoped>
.tool-call-details { overflow-wrap: anywhere; }
</style>
