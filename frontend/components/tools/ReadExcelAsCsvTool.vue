<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-table-cells" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="isRunning">
        Reading {{ rangeLabel }} as CSV…
      </template>
      <template v-else-if="succeeded">Read {{ rangeLabel }} as CSV</template>
      <template v-else>CSV read failed</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="isRunning"></template>
      <template v-else-if="succeeded"><span v-if="truncated" class="ms-1.5 text-chat-3 px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-700">truncated</span></template>
      <template v-else><span v-if="errorMessage" class="ms-1.5 text-chat-2 text-red-600 truncate max-w-[320px]">{{ errorMessage }}</span></template></div>

    <Transition name="fade">
      <div v-if="toolHeaderExpanded && hasDetail" class="mt-2 ms-4 text-chat-2 text-gray-600 dark:text-gray-400">
        <div class="text-chat-3 text-ink-muted mb-0.5">CSV</div>
        <pre class="code-block text-chat-2 text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 rounded p-2 overflow-x-auto"><code>{{ csvPreview }}</code></pre>
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

interface ToolExecution {
  id: string
  tool_name: string
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

const sheetName = computed<string>(() => aj.value?.sheet_name || '')
const range = computed<string>(() => aj.value?.range || '')

const isRunning = computed<boolean>(() => status.value === 'running' || (!('success' in rj.value) && status.value !== 'success' && status.value !== 'error' && status.value !== 'stopped'))
const succeeded = computed<boolean>(() => !isRunning.value && rj.value?.success === true)
const errorMessage = computed<string>(() => rj.value?.error || props.toolExecution?.result_summary || '')
const truncated = computed<boolean>(() => !!rj.value?.truncated)

const rangeLabel = computed<string>(() => {
  if (!sheetName.value) return 'Excel range'
  return range.value ? `${sheetName.value}!${range.value}` : sheetName.value
})

const csv = computed<string>(() => rj.value?.csv || '')

const csvPreview = computed<string>(() => {
  const s = csv.value
  if (!s) return ''
  const MAX_CHARS = 4000
  const MAX_LINES = 40
  let out = s.length > MAX_CHARS ? s.slice(0, MAX_CHARS) + '\n…' : s
  const lines = out.split('\n')
  if (lines.length > MAX_LINES) out = lines.slice(0, MAX_LINES).join('\n') + '\n…'
  return out
})

const hasDetail = computed<boolean>(() => !!csv.value)

const collapsed = ref(true)
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
