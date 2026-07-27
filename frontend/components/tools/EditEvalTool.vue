<template>
  <div class="mt-1">
    <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-pencil-square" class="w-3 h-3 me-1 text-gray-400" />
        {{ t('tools.editEval.editing') }}
      </span>
      <span v-else-if="success" class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-pencil-square" class="w-3 h-3 me-1 text-purple-400" />
        <span class="align-middle">{{ t('tools.editEval.edited') }}</span>
      </span>
      <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">{{ message || t('tools.editEval.failed') }}</span>
      </span>
    </div>

    <div v-if="success" class="text-xs text-gray-600 dark:text-gray-400">
      <div class="flex items-center py-1 px-1 rounded">
        <Icon name="heroicons-beaker" class="w-3 h-3 me-1 text-purple-400 flex-shrink-0" />
        <NuxtLink :to="`/evals`" class="font-medium text-gray-700 dark:text-gray-300 truncate hover:text-blue-600">
          {{ caseName || t('tools.editEval.unnamed') }}
        </NuxtLink>
        <span v-if="suiteName" class="ms-1.5 text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">{{ suiteName }}</span>
        <span v-if="caseStatus === 'draft'" class="ms-1 text-[9px] px-1 py-0.5 rounded bg-amber-100 text-amber-800 flex-shrink-0">{{ t('tools.editEval.statusDraft') }}</span>
        <span v-else-if="caseStatus === 'active'" class="ms-1 text-[9px] px-1 py-0.5 rounded bg-green-100 dark:bg-green-900/50 text-green-800 flex-shrink-0">{{ t('tools.editEval.statusActive') }}</span>
        <span v-else-if="caseStatus === 'archived'" class="ms-1 text-[9px] px-1 py-0.5 rounded bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 flex-shrink-0">{{ t('tools.editEval.statusArchived') }}</span>
      </div>
      <div v-if="changedFields.length" class="ms-1 mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
        {{ t('tools.editEval.changed', { fields: changedFields.join(', ') }) }}
      </div>
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
const caseName = computed<string>(() => result.value?.name || '')
const suiteName = computed<string>(() => result.value?.suite_name || '')
const caseStatus = computed<string>(() => result.value?.status || '')
const message = computed<string>(() => result.value?.message || '')
const changedFields = computed<string[]>(() => Array.isArray(result.value?.changed_fields) ? result.value.changed_fields : [])
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
