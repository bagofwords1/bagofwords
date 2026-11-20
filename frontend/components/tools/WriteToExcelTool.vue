<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-table-cells" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="isRunning">
        Writing {{ rowLabel }} to Excel…
      </template>
      <template v-else-if="succeeded">Wrote {{ rowLabel }} to Excel<template v-if="wroteTo"> at {{ wroteTo }}</template></template>
      <template v-else>Couldn't write to Excel</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="isRunning"></template>
      <template v-else-if="succeeded"></template>
      <template v-else><span v-if="errorMessage" class="ms-1.5 text-chat-2 text-red-600 truncate max-w-[320px]">{{ errorMessage }}</span></template></div>

    <Transition name="fade">
      <div v-if="toolHeaderExpanded && hasDetail" class="mt-2 ms-4 text-chat-2 text-gray-600 dark:text-gray-400">
        <div v-if="title" class="mb-1 text-gray-700 dark:text-gray-300"><span class="text-ink-muted">Title:</span> {{ title }}</div>
        <div v-if="columnNames.length" class="mb-1">
          <span class="text-ink-muted">Columns:</span>
          <span class="ms-1">{{ columnNames.join(', ') }}</span>
        </div>
        <div v-if="rowPreview.length" class="mt-2">
          <div class="text-chat-3 text-ink-muted mb-0.5">Preview ({{ rowPreview.length }} of {{ rowCount }} rows)</div>
          <pre class="code-block text-chat-2 text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 rounded p-2 overflow-x-auto"><code>{{ previewText }}</code></pre>
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

const isRunning = computed<boolean>(() => status.value === 'running' || (!('success' in rj.value) && status.value !== 'success' && status.value !== 'error' && status.value !== 'stopped'))
const succeeded = computed<boolean>(() => !isRunning.value && (rj.value?.success === true || status.value === 'success'))
const errorMessage = computed<string>(() => rj.value?.error_message || rj.value?.error || props.toolExecution?.result_summary || '')

const title = computed<string>(() => aj.value?.title || '')

const wroteTo = computed<string>(() => rj.value?.wrote_to || '')

const rowCount = computed<number>(() => {
  if (typeof rj.value?.row_count === 'number') return rj.value.row_count
  const rows = rj.value?.excel_action?.data?.widget?.last_step?.data?.rows
  if (Array.isArray(rows)) return rows.length
  return Array.isArray(aj.value?.rows) ? aj.value.rows.length : 0
})

const columnCount = computed<number>(() => {
  if (typeof rj.value?.column_count === 'number') return rj.value.column_count
  const cols = rj.value?.excel_action?.data?.widget?.last_step?.data?.columns
  if (Array.isArray(cols)) return cols.length
  return Array.isArray(aj.value?.columns) ? aj.value.columns.length : 0
})

const rowLabel = computed<string>(() => {
  const r = rowCount.value
  const c = columnCount.value
  if (!r && !c) return 'data'
  return `${r} row${r === 1 ? '' : 's'} × ${c} col${c === 1 ? '' : 's'}`
})

const columns = computed<any[]>(() => {
  const fromResult = rj.value?.excel_action?.data?.widget?.last_step?.data?.columns
  if (Array.isArray(fromResult)) return fromResult
  return Array.isArray(aj.value?.columns) ? aj.value.columns : []
})

const columnNames = computed<string[]>(() =>
  columns.value
    .map((c: any) => c?.headerName || c?.field || '')
    .filter(Boolean)
)

const rows = computed<any[]>(() => Array.isArray(aj.value?.rows) ? aj.value.rows : [])

const rowPreview = computed<any[]>(() => rows.value.slice(0, 5))

const previewText = computed<string>(() => {
  const fields = columns.value.map((c: any) => c?.field).filter(Boolean)
  if (!fields.length || !rowPreview.value.length) return ''
  const header = fields.join('\t')
  const lines = rowPreview.value.map((row: any) =>
    fields.map((f: string) => {
      const v = row?.[f]
      if (v === null || v === undefined) return ''
      const s = typeof v === 'string' ? v : JSON.stringify(v)
      return s.length > 40 ? s.slice(0, 37) + '…' : s
    }).join('\t')
  )
  return [header, ...lines].join('\n')
})

const hasDetail = computed<boolean>(() => !!title.value || columnNames.value.length > 0 || rowPreview.value.length > 0)

const collapsed = ref(true)
</script>

<style scoped>
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.25s ease, transform 0.25s ease;
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
