<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="status === 'running'" class="flex items-center">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
          <span class="tool-shimmer">{{ modelTitle ? modelTitle + '…' : 'Reading ' + fileLabel + '…' }}</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-document-arrow-down" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ modelTitle || ('Read ' + fileLabel) }}</span>
          <span v-if="contentType" class="ms-2 text-[10px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400">{{ contentType }}</span>
          <span v-if="rowCount != null" class="ms-2 text-gray-400">{{ rowCount }} rows × {{ colCount }} cols</span>
          <span v-if="truncated" class="ms-2 text-[10px] text-yellow-600">truncated</span>
        </span>
      </div>
    </Transition>

    <Transition name="fade" appear>
      <div v-if="hasContent" class="text-xs text-gray-600 dark:text-gray-400">
        <div
          class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
          @click="expanded = !expanded"
        >
          <Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 me-1 rtl-flip" />
          <span class="text-gray-500 dark:text-gray-400">Preview</span>
        </div>
        <Transition name="fade">
          <div v-if="expanded" class="ps-6 pe-1 pb-1">
            <pre class="text-[11px] bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded p-2 max-h-64 overflow-auto whitespace-pre-wrap">{{ previewText }}</pre>
            <div v-if="sessionFileId" class="mt-2 text-[11px] text-gray-500 dark:text-gray-400">
              <Icon name="heroicons-paper-clip" class="w-3 h-3 inline align-text-bottom me-0.5" />
              Attached to this conversation as session file
              <code class="ms-1 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">{{ sessionFileId.slice(0, 8) }}…</code>
            </div>
          </div>
        </Transition>
      </div>
    </Transition>

    <div v-if="status !== 'running' && !hasContent && errorMessage" class="text-xs text-red-600 mt-1">{{ errorMessage }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed(() => props.toolExecution?.status || '')

const modelTitle = computed<string>(() => {
  const t = props.toolExecution?.arguments_json?.title
  return typeof t === 'string' && t.trim() ? t.trim() : ''
})
const rj = computed<any>(() => props.toolExecution?.result_json || {})

const fileLabel = computed(() => {
  return rj.value.file_name
    || props.toolExecution?.arguments_json?.file_id?.slice(0, 8)
    || 'file'
})
const contentType = computed(() => rj.value.content_type || '')
const rowCount = computed(() => rj.value.row_count)
const colCount = computed(() => rj.value.col_count)
const truncated = computed(() => !!rj.value.truncated)
const sessionFileId = computed(() => rj.value.session_file_id || '')
const errorMessage = computed(() => rj.value.error || '')

const hasContent = computed(() => !!(rj.value.csv || rj.value.text || rj.value.byte_count))
const previewText = computed(() => {
  if (rj.value.csv) return String(rj.value.csv).slice(0, 4000)
  if (rj.value.text) return String(rj.value.text).slice(0, 4000)
  if (rj.value.byte_count) return `(binary, ${rj.value.byte_count} bytes)`
  return ''
})

const expanded = ref(false)
</script>

<style scoped>
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
}
@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
