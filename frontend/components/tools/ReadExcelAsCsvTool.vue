<template>
  <div class="mt-1">
    <div
      class="flex items-center text-xs"
      :class="hasDetail ? 'cursor-pointer hover:text-gray-700' : ''"
      @click="hasDetail && (collapsed = !collapsed)"
    >
      <Icon
        v-if="hasDetail"
        :name="collapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'"
        class="w-3 h-3 mr-1 text-gray-400"
      />
      <span v-if="isRunning" class="tool-shimmer flex items-center text-gray-500">
        <Icon name="heroicons-document-text" class="w-3 h-3 mr-1 text-gray-400" />
        Reading {{ rangeLabel }} as CSV…
      </span>
      <span v-else-if="succeeded" class="text-gray-700 flex items-center">
        <Icon name="heroicons-check" class="w-3 h-3 mr-1 text-green-500" />
        <span class="align-middle">Read {{ rangeLabel }} as CSV</span>
        <span v-if="truncated" class="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-amber-50 text-amber-700">truncated</span>
      </span>
      <span v-else class="text-red-500 flex items-center">
        <Icon name="heroicons-exclamation-circle" class="w-3 h-3 mr-1" />
        <span class="align-middle">CSV read failed</span>
        <span v-if="errorMessage" class="ml-1.5 text-[11px] text-red-600 truncate max-w-[320px]">{{ errorMessage }}</span>
      </span>
    </div>

    <Transition name="fade">
      <div v-if="!collapsed && hasDetail" class="mt-2 ml-4 text-xs text-gray-600">
        <div class="text-[10px] text-gray-400 mb-0.5">CSV</div>
        <pre class="code-block text-[11px] text-gray-700 bg-gray-50 border border-gray-100 rounded p-2 overflow-x-auto"><code>{{ csvPreview }}</code></pre>
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
