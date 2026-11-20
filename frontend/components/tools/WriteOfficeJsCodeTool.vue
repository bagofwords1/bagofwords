<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-command-line" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="isRunning">
        Running Excel code…{{ description ? ' ' + description : '' }}</template>
      <template v-else-if="succeeded">{{ t('tools.writeOfficeJs.ran') }}</template>
      <template v-else>{{ t('tools.writeOfficeJs.failed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="isRunning"></template>
      <template v-else-if="succeeded"><span v-if="description" class="ms-1.5 text-chat-2 text-ink-muted truncate max-w-[320px]">· {{ description }}</span></template>
      <template v-else><span v-if="isSyntaxError" class="ms-1.5 text-chat-3 px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300 flex-shrink-0">syntax</span><span v-if="errorMessage" class="ms-1.5 text-chat-2 text-red-600 truncate max-w-[320px]">{{ errorMessage }}</span></template></div>

    <Transition name="fade">
      <div v-if="toolHeaderExpanded && hasDetail" class="mt-2 ms-4 text-chat-2 text-gray-600 dark:text-gray-400">
        <pre v-if="code" class="code-block text-chat-2 text-gray-800 dark:text-gray-200 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 rounded p-2 overflow-x-auto"><code>{{ code }}</code></pre>
        <div v-if="logs && logs.length" class="mt-2">
          <div class="text-chat-3 text-ink-muted mb-0.5">Logs ({{ logs.length }})</div>
          <pre class="code-block text-chat-2 text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 rounded p-2 overflow-x-auto"><code>{{ logs.join('\n') }}</code></pre>
        </div>
        <div v-if="returnValueFormatted" class="mt-2">
          <div class="text-chat-3 text-ink-muted mb-0.5">Returned</div>
          <pre class="code-block text-chat-2 text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 rounded p-2 overflow-x-auto"><code>{{ returnValueFormatted }}</code></pre>
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

const { t } = useResponseI18n()

import { computed, ref, watch } from 'vue'

interface ToolExecution {
  id: string
  tool_name: string
  tool_action?: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const status = computed<string>(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})
const aj = computed<any>(() => props.toolExecution?.arguments_json || {})

const code = computed<string>(() => aj.value?.code || '')
const description = computed<string>(() => aj.value?.description || '')

const isRunning = computed<boolean>(() => status.value === 'running' || (!('success' in rj.value) && status.value !== 'success' && status.value !== 'error' && status.value !== 'stopped'))
const succeeded = computed<boolean>(() => !isRunning.value && rj.value?.success === true)
const errorMessage = computed<string>(() => rj.value?.error || props.toolExecution?.result_summary || '')
const isSyntaxError = computed<boolean>(() => typeof errorMessage.value === 'string' && errorMessage.value.startsWith('SyntaxError:'))

const logs = computed<string[]>(() => Array.isArray(rj.value?.logs) ? rj.value.logs : [])

const returnValueFormatted = computed<string>(() => {
  const rv = rj.value?.return_value
  if (rv === undefined || rv === null) return ''
  try { return typeof rv === 'string' ? rv : JSON.stringify(rv, null, 2) } catch { return String(rv) }
})

const hasDetail = computed<boolean>(() => !!code.value || logs.value.length > 0 || !!returnValueFormatted.value)

const collapsed = ref(true)
watch(
  () => [isRunning.value, succeeded.value, errorMessage.value],
  () => {
    if (!isRunning.value && !succeeded.value && hasDetail.value) collapsed.value = false
  },
  { immediate: true },
)
</script>

<style scoped>
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
  transform: translateY(2px);
}

.code-block {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 240px;
  overflow-y: auto;
}
</style>
