<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-clipboard-document-check" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.editEval.editing') }}</template>
      <template v-else-if="success">{{ t('tools.editEval.edited') }}</template>
      <template v-else>{{ t('tools.editEval.failed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <p v-if="message" class="mb-2 text-chat-3 text-ink-muted break-words">{{ message }}</p>

    <div v-if="success" class="text-chat-2 text-gray-600 dark:text-gray-400">
      <div class="flex items-center py-1 px-1 rounded">
        <Icon name="heroicons-beaker" class="w-3 h-3 me-1 text-purple-400 flex-shrink-0" />
        <NuxtLink :to="`/agents`" class="font-medium text-gray-700 dark:text-gray-300 truncate hover:text-blue-600">
          {{ caseName || t('tools.editEval.unnamed') }}
        </NuxtLink>
        <span v-if="suiteName" class="ms-1.5 text-chat-3 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">{{ suiteName }}</span>
        <span v-if="caseStatus === 'draft'" class="ms-1 text-chat-3 px-1 py-0.5 rounded bg-amber-100 text-amber-800 flex-shrink-0">{{ t('tools.editEval.statusDraft') }}</span>
        <span v-else-if="caseStatus === 'active'" class="ms-1 text-chat-3 px-1 py-0.5 rounded bg-green-100 dark:bg-green-900/50 text-green-800 flex-shrink-0">{{ t('tools.editEval.statusActive') }}</span>
        <span v-else-if="caseStatus === 'archived'" class="ms-1 text-chat-3 px-1 py-0.5 rounded bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 flex-shrink-0">{{ t('tools.editEval.statusArchived') }}</span>
      </div>
      <div v-if="changedFields.length" class="ms-1 mt-0.5 text-chat-2 text-gray-500 dark:text-gray-400">
        {{ t('tools.editEval.changed', { fields: changedFields.join(', ') }) }}
      </div>
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
const caseName = computed<string>(() => result.value?.name || '')
const suiteName = computed<string>(() => result.value?.suite_name || '')
const caseStatus = computed<string>(() => result.value?.status || '')
const message = computed<string>(() => result.value?.message || '')
const changedFields = computed<string[]>(() => Array.isArray(result.value?.changed_fields) ? result.value.changed_fields : [])
</script>

<style scoped>
.tool-call-details { overflow-wrap: anywhere; }
</style>
