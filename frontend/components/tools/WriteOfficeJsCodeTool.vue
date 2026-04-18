<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500">
        <span v-if="isRunning" class="tool-shimmer flex items-center">
          <Icon name="heroicons-code-bracket" class="w-3 h-3 mr-1 text-gray-400" />
          Running Excel code…{{ description ? ' ' + description : '' }}
        </span>
        <span v-else-if="succeeded" class="text-gray-700 flex items-center">
          <Icon name="heroicons-code-bracket" class="w-3 h-3 mr-1 text-gray-400" />
          <span class="align-middle">Ran Excel code</span>
          <span v-if="returnPreview" class="ml-1.5 text-[10px] text-gray-400">· returned {{ returnPreview }}</span>
        </span>
        <span v-else class="text-red-500 flex items-center">
          <Icon name="heroicons-exclamation-circle" class="w-3 h-3 mr-1" />
          <span class="align-middle">Excel code failed</span>
          <span v-if="isSyntaxError" class="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-amber-50 text-amber-700 flex-shrink-0">syntax</span>
          <span v-if="errorMessage" class="ml-1.5 text-[11px] text-red-600 truncate max-w-[320px]">{{ errorMessage }}</span>
        </span>
      </div>
    </Transition>

    <div v-if="hasDetail" class="text-xs text-gray-600">
      <button
        class="flex items-center py-0.5 px-1 rounded hover:bg-gray-50 text-[11px] text-gray-500"
        @click="expanded = !expanded"
      >
        <Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 mr-1" />
        {{ expanded ? 'Hide code' : 'Show code' }}
      </button>
      <Transition name="fade">
        <div v-if="expanded" class="pl-5 pr-1 pb-2">
          <pre v-if="code" class="code-block text-[11px] text-gray-800 bg-gray-50 border border-gray-100 rounded p-2 overflow-x-auto"><code>{{ code }}</code></pre>
          <div v-if="logs && logs.length" class="mt-2">
            <div class="text-[10px] text-gray-400 mb-0.5">Logs ({{ logs.length }})</div>
            <pre class="code-block text-[11px] text-gray-700 bg-gray-50 border border-gray-100 rounded p-2 overflow-x-auto"><code>{{ logs.join('\n') }}</code></pre>
          </div>
          <div v-if="returnValueFormatted" class="mt-2">
            <div class="text-[10px] text-gray-400 mb-0.5">Returned</div>
            <pre class="code-block text-[11px] text-gray-700 bg-gray-50 border border-gray-100 rounded p-2 overflow-x-auto"><code>{{ returnValueFormatted }}</code></pre>
          </div>
        </div>
      </Transition>
    </div>
  </div>
</template>

<script setup lang="ts">
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

// The tool emits tool.partial with excel_action immediately, then waits for the
// taskpane to post back a result before tool.end resolves. So "running" covers
// both the pre-dispatch state and the iframe-waiting state.
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

const returnPreview = computed<string>(() => {
  const rv = rj.value?.return_value
  if (rv === undefined || rv === null) return ''
  try {
    const s = typeof rv === 'string' ? rv : JSON.stringify(rv)
    return s.length > 60 ? s.slice(0, 57) + '…' : s
  } catch {
    return ''
  }
})

const hasDetail = computed<boolean>(() => !!code.value || logs.value.length > 0 || !!returnValueFormatted.value)

const expanded = ref(false)
// Auto-open on failure so the user can see the code + error immediately.
watch(
  () => [isRunning.value, succeeded.value, errorMessage.value],
  () => {
    if (!isRunning.value && !succeeded.value && hasDetail.value) expanded.value = true
  },
  { immediate: true },
)
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
