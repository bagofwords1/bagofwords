<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div
        class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
        @click="toggleExpanded"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-pencil-square" class="w-3 h-3 me-1.5 text-gray-400" />
          <span>Updating prompt…</span>
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-pencil-square" class="w-3 h-3 me-1.5 text-green-500" />
          <span dir="auto" class="truncate max-w-[300px]">Updated: {{ promptTitle }}</span>
          <span v-if="changedFields.length" class="ms-1.5 text-[10px] text-gray-400 shrink-0">· {{ changedFields.join(', ') }}</span>
          <Icon :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 ms-1 text-gray-400 shrink-0 rtl-flip" />
        </span>
        <span v-else-if="isRejected" class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-orange-500" />
          <span>Prompt not updated</span>
          <span v-if="rejectedReason" class="ms-1.5 text-orange-600 text-[10px]">({{ rejectedReason }})</span>
          <Icon :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 ms-1 text-gray-400 rtl-flip" />
        </span>
        <span v-else class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-red-500" />
          <span>Failed to update prompt</span>
          <Icon :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 ms-1 text-gray-400 rtl-flip" />
        </span>
      </div>
    </Transition>

    <!-- Expandable content -->
    <Transition name="slide">
      <div v-if="isExpanded && status !== 'running'" class="mt-2 space-y-2">
        <div class="hover:bg-gray-50 dark:hover:bg-gray-800 border border-gray-150 dark:border-gray-700 rounded-md p-3 transition-colors">
          <div v-if="newText" dir="auto" class="prompt-content text-[12px] text-gray-800 dark:text-gray-200 leading-relaxed mb-2">
            <MDC :value="newText" class="markdown-content" />
          </div>
          <div class="flex flex-wrap items-center gap-2 text-[10px] mb-1">
            <span class="text-gray-500 dark:text-gray-400">Changed:</span>
            <span v-for="f in changedFields" :key="f" class="px-1.5 py-0.5 rounded text-[9px] font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">{{ f }}</span>
            <span v-if="!changedFields.length" class="text-gray-400">no fields</span>
          </div>
          <div v-if="isSuccess" class="flex items-center gap-1.5 pt-2 border-t border-gray-200 dark:border-gray-700">
            <Icon name="heroicons:check-circle" class="w-3 h-3 text-green-500" />
            <span class="text-[10px] font-medium text-gray-600 dark:text-gray-400">Live</span>
            <button class="ms-auto text-[10px] text-blue-600 hover:text-blue-800 inline-flex items-center gap-0.5" @click.stop="openPrompts">
              <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
              <span>Open in Prompts</span>
            </button>
          </div>
          <div v-if="errorMessage" class="text-[10px] text-orange-600 bg-orange-50/50 dark:bg-orange-950 rounded px-2 py-1 mt-2">
            {{ errorMessage }}
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

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
const changedFields = computed(() => {
  const keys = ['text', 'title', 'scope', 'mode', 'is_starter', 'data_source_ids', 'parameters']
  return keys.filter((k) => args.value[k] !== undefined && args.value[k] !== null)
    .map((k) => (k === 'is_starter' ? 'starter' : k === 'data_source_ids' ? 'agents' : k))
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
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer { 0% { background-position: 0% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
.slide-enter-active, .slide-leave-active { transition: all 0.15s ease; overflow: hidden; }
.slide-enter-from, .slide-leave-to { opacity: 0; max-height: 0; }
.slide-enter-to, .slide-leave-from { opacity: 1; max-height: 500px; }
.prompt-content :deep(.markdown-content) { font-size: 12px; line-height: 1.5; }
.prompt-content :deep(.markdown-content p) { margin: 0 0 0.5em 0; }
.prompt-content :deep(.markdown-content p:last-child) { margin-bottom: 0; }
.prompt-content :deep(.markdown-content code) { font-size: 10px; padding: 0.1em 0.3em; background: rgba(0,0,0,0.05); border-radius: 3px; }
</style>
