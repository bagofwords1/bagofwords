<template>
  <div class="min-w-0">
    <!-- Status header -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-document-text" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">
      <template v-if="promptTitle">{{ t('tools.createPrompt.savingPrefix', { title: promptTitle }) }}</template>
      <template v-else>{{ t('tools.createPrompt.saving') }}</template></template>
      <template v-else-if="isSuccess">{{ promptTitle }}</template>
      <template v-else-if="isRejected">{{ t('tools.createPrompt.notSaved') }}</template>
      <template v-else>{{ t('tools.createPrompt.failed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'"></template>
      <template v-else-if="isSuccess"><span class="ms-1.5 px-1.5 py-0.5 bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 rounded text-chat-3 shrink-0">{{ scope }}</span><span v-if="isStarter" class="ms-1 px-1.5 py-0.5 bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 rounded text-chat-3 shrink-0">{{ t('tools.createPrompt.starter') }}</span><span v-if="paramCount > 0" class="ms-1 px-1.5 py-0.5 bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-400 rounded text-chat-3 shrink-0">{{ t('tools.createPrompt.paramsBadge', { n: paramCount }) }}</span></template>
      <template v-else-if="isRejected"><span v-if="rejectedReason" class="ms-1.5 text-orange-600 text-chat-3">({{ rejectedReason }})</span></template>
      <template v-else></template></div>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="toolHeaderExpanded && (status !== 'running' || promptText)" class="mt-2 space-y-2">
        <div class="hover:bg-gray-50 dark:hover:bg-gray-800 border border-gray-150 dark:border-gray-700 rounded-md p-3 transition-colors">
          <!-- Prompt text -->
          <div v-if="promptText" class="prompt-content text-chat-2 text-gray-800 dark:text-gray-200 leading-relaxed mb-2">
            <MDC :value="promptText" class="markdown-content" />
          </div>

          <!-- Metadata row -->
          <div class="flex flex-wrap items-center gap-2 text-chat-3 mb-1">
            <div class="flex items-center gap-1">
              <span class="text-gray-500 dark:text-gray-400">{{ t('tools.createPrompt.scope') }}</span>
              <span class="px-1.5 py-0.5 rounded text-chat-3 font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">{{ scope }}</span>
            </div>
            <div v-if="mode && mode !== 'chat'" class="flex items-center gap-1">
              <span class="text-gray-500 dark:text-gray-400">{{ t('tools.createPrompt.mode') }}</span>
              <span class="px-1.5 py-0.5 rounded text-chat-3 font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">{{ mode }}</span>
            </div>
            <div v-if="isStarter" class="flex items-center gap-1">
              <Icon name="heroicons-sparkles" class="w-3 h-3 text-amber-500" />
              <span class="text-gray-600 dark:text-gray-400">{{ t('tools.createPrompt.conversationStarter') }}</span>
            </div>
            <div v-if="agentCount > 0" class="flex items-center gap-1">
              <Icon name="heroicons-cube" class="w-3 h-3 text-ink-muted" />
              <span class="text-gray-600 dark:text-gray-400">{{ agentCount === 1 ? t('tools.createPrompt.agentSingular', { n: agentCount }) : t('tools.createPrompt.agentPlural', { n: agentCount }) }}</span>
            </div>
          </div>

          <!-- Parameters -->
          <div v-if="paramCount > 0" class="flex flex-wrap items-center gap-1 mb-2">
            <span class="text-chat-3 text-gray-500 dark:text-gray-400">{{ t('tools.createPrompt.parameters') }}</span>
            <code v-for="chip in paramChips" :key="chip" class="text-chat-3 px-1 py-0.5 bg-indigo-50 dark:bg-indigo-950 text-indigo-600 dark:text-indigo-400 rounded">{{ chip }}</code>
          </div>

          <!-- Live + open -->
          <div v-if="isSuccess" class="flex items-center gap-1.5 pt-2 border-t border-gray-200 dark:border-gray-700">
            <Icon name="heroicons:check-circle" class="w-3 h-3 text-green-500" />
            <span class="text-chat-3 font-medium text-gray-600 dark:text-gray-400">{{ t('tools.createPrompt.live') }}</span>
            <button class="ms-auto text-chat-2 text-blue-600 hover:text-blue-800 inline-flex items-center gap-0.5" @click.stop="openPrompts">
              <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
              <span>{{ t('tools.createPrompt.openInPrompts') }}</span>
            </button>
          </div>

          <!-- Error / rejection message -->
          <div v-if="errorMessage" class="text-chat-2 text-orange-600 bg-orange-50/50 dark:bg-orange-950 rounded px-2 py-1 mt-2">
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

const isSuccess = computed(() => status.value === 'success' && (props.toolExecution?.result_json || {}).success === true)
const isRejected = computed(() => {
  const rj = props.toolExecution?.result_json || {}
  return status.value === 'success' && rj.success === false && rj.rejected_reason
})

const args = computed(() => props.toolExecution?.arguments_json || {})
const result = computed(() => props.toolExecution?.result_json || {})

const promptText = computed(() => args.value.text || '')
const promptTitle = computed(() => {
  const t = result.value.title || args.value.title
  if (t) return t
  const txt = String(promptText.value || '').trim()
  const firstLine = txt.split('\n')[0].replace(/^#+\s*/, '').trim()
  return firstLine.length > 60 ? firstLine.slice(0, 57) + '…' : (firstLine || t('tools.createPrompt.saving'))
})
const scope = computed(() => result.value.scope || args.value.scope || 'agent')
const mode = computed(() => args.value.mode || 'chat')
const isStarter = computed(() => result.value.is_starter ?? args.value.is_starter ?? false)
const agentCount = computed(() => {
  const ids = result.value.data_source_ids || args.value.data_source_ids || []
  return Array.isArray(ids) ? ids.length : 0
})
const paramNames = computed(() => (Array.isArray(args.value.parameters) ? args.value.parameters.map((p: any) => p?.name).filter(Boolean) : []))
const paramCount = computed(() => paramNames.value.length)
const paramChips = computed(() => paramNames.value.map((p: string) => '{' + '{' + p + '}' + '}'))
const rejectedReason = computed(() => result.value.rejected_reason || '')
const errorMessage = computed(() => {
  if (status.value === 'error') return result.value.error || result.value.message || 'An error occurred'
  if (isRejected.value) return result.value.message || ''
  return ''
})

function toggleExpanded() {
  if (status.value !== 'running' || promptText.value) isExpanded.value = !isExpanded.value
}
function openPrompts() {
  navigateTo('/prompts')
}
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
