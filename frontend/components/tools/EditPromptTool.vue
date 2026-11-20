<template>
  <div class="min-w-0">
    <!-- Status header -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-document-text" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.editPrompt.updating') }}</template>
      <template v-else-if="isSuccess">{{ t('tools.editPrompt.updatedPrefix', { title: promptTitle }) }}</template>
      <template v-else-if="isRejected">{{ t('tools.editPrompt.notUpdated') }}</template>
      <template v-else>{{ t('tools.editPrompt.failed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'"></template>
      <template v-else-if="isSuccess"><span v-if="changedFields.length" class="ms-1.5 text-chat-3 text-ink-muted shrink-0">· {{ changedFields.join(', ') }}</span></template>
      <template v-else-if="isRejected"><span v-if="rejectedReason" class="ms-1.5 text-orange-600 text-chat-3">({{ rejectedReason }})</span></template>
      <template v-else></template></div>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="toolHeaderExpanded && status !== 'running'" class="mt-2 space-y-2">
        <div class="hover:bg-gray-50 dark:hover:bg-gray-800 border border-gray-150 dark:border-gray-700 rounded-md p-3 transition-colors">
          <div v-if="newText" class="prompt-content text-chat-2 text-gray-800 dark:text-gray-200 leading-relaxed mb-2">
            <MDC :value="newText" class="markdown-content" />
          </div>
          <div class="flex flex-wrap items-center gap-2 text-chat-3 mb-1">
            <span class="text-gray-500 dark:text-gray-400">{{ t('tools.editPrompt.changed') }}</span>
            <span v-for="f in changedFields" :key="f" class="px-1.5 py-0.5 rounded text-chat-3 font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">{{ f }}</span>
            <span v-if="!changedFields.length" class="text-ink-muted">{{ t('tools.editPrompt.noFields') }}</span>
          </div>
          <div v-if="isSuccess" class="flex items-center gap-1.5 pt-2 border-t border-gray-200 dark:border-gray-700">
            <Icon name="heroicons:check-circle" class="w-3 h-3 text-green-500" />
            <span class="text-chat-3 font-medium text-gray-600 dark:text-gray-400">{{ t('tools.editPrompt.live') }}</span>
            <button class="ms-auto text-chat-2 text-blue-600 hover:text-blue-800 inline-flex items-center gap-0.5" @click.stop="openPrompts">
              <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
              <span>{{ t('tools.editPrompt.openInPrompts') }}</span>
            </button>
          </div>
          <div v-if="errorMessage" class="text-chat-2 text-orange-600 dark:text-orange-300 bg-orange-50/50 dark:bg-orange-950 rounded px-2 py-1 mt-2">
            {{ errorMessage }}
          </div>
        </div>
      </div>
    </Transition>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed, ref } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
const { t } = useResponseI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}
const props = defineProps<{ toolExecution: ToolExecution }>()

const isExpanded = ref(true)
const status = computed<string>(() => props.toolExecution?.status || '')
const args = computed(() => props.toolExecution?.arguments_json || {})
const result = computed(() => props.toolExecution?.result_json || {})

const isSuccess = computed(() => status.value === 'success' && result.value.success === true)
const isRejected = computed(() => status.value === 'success' && result.value.success === false && result.value.rejected_reason)

const promptTitle = computed(() => result.value.title || args.value.title || 'prompt')
const newText = computed(() => args.value.text || '')
const FIELD_LABEL: Record<string, string> = {
  text: 'fieldText', title: 'fieldTitle', scope: 'fieldScope', mode: 'fieldMode',
  is_starter: 'fieldStarter', data_source_ids: 'fieldAgents', parameters: 'fieldParameters',
}
const changedFields = computed(() => {
  const keys = ['text', 'title', 'scope', 'mode', 'is_starter', 'data_source_ids', 'parameters']
  return keys.filter((k) => args.value[k] !== undefined && args.value[k] !== null)
    .map((k) => t('tools.editPrompt.' + FIELD_LABEL[k]))
})
const rejectedReason = computed(() => result.value.rejected_reason || '')
const errorMessage = computed(() => {
  if (status.value === 'error') return result.value.error || result.value.message || 'An error occurred'
  if (isRejected.value) return result.value.message || ''
  return ''
})

function toggleExpanded() { if (status.value !== 'running') isExpanded.value = !isExpanded.value }
function openPrompts() { navigateTo('/prompts') }
</script>

<style scoped>
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
.slide-enter-active, .slide-leave-active { transition: all 0.15s ease; overflow: hidden; }
.slide-enter-from, .slide-leave-to { opacity: 0; max-height: 0; }
.slide-enter-to, .slide-leave-from { opacity: 1; max-height: 500px; }
.prompt-content :deep(.markdown-content) { font-size: var(--bow-chat-font-size-2); line-height: 1.5; }
.prompt-content :deep(.markdown-content p) { margin: 0 0 0.5em 0; }
.prompt-content :deep(.markdown-content p:last-child) { margin-bottom: 0; }
.prompt-content :deep(.markdown-content code) { font-size: 0.9em; padding: 0.1em 0.3em; background: rgba(0,0,0,0.05); border-radius: 3px; }
</style>
