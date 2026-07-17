<template>
  <div class="mt-1">
    <div class="mb-1 flex items-center text-xs text-gray-500 dark:text-gray-400">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-stop-circle" class="w-3 h-3 me-1 text-gray-400" />
        {{ t('tools.stopEvalRun.stopping') }}
      </span>
      <span v-else-if="success" class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-stop-circle" class="w-3 h-3 me-1 text-red-400" />
        <span class="align-middle">{{ t('tools.stopEvalRun.stopped') }}</span>
        <span v-if="total" class="ms-1.5 text-[10px] text-gray-500 dark:text-gray-400">{{ passed }}/{{ total }} {{ t('tools.stopEvalRun.passedAtStop') }}</span>
      </span>
      <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">{{ message || t('tools.stopEvalRun.failed') }}</span>
      </span>
    </div>
    <div v-if="runId" class="text-[10px] text-gray-400 ms-1">
      <NuxtLink :to="`/evals/runs/${runId}`" class="hover:text-blue-600 inline-flex items-center gap-0.5">
        <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
        {{ t('tools.stopEvalRun.openRun') }}
      </NuxtLink>
    </div>
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
const success = computed<boolean>(() => !!result.value?.success)
const runId = computed<string>(() => result.value?.run_id || '')
const total = computed<number>(() => result.value?.total || 0)
const passed = computed<number>(() => result.value?.passed || 0)
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
