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
        class="w-3 h-3 me-1 text-gray-400"
      />
      <span v-if="isRunning" class="tool-shimmer flex items-center text-gray-500">
        <Icon name="heroicons-table-cells" class="w-3 h-3 me-1 text-gray-400" />
        Writing {{ rowLabel }} to Excel…
      </span>
      <span v-else-if="succeeded" class="text-gray-700 flex items-center">
        <Icon name="heroicons-check" class="w-3 h-3 me-1 text-green-500" />
        <span class="align-middle">Wrote {{ rowLabel }} to Excel</span>
      </span>
      <span v-else class="text-red-500 flex items-center">
        <Icon name="heroicons-exclamation-circle" class="w-3 h-3 me-1" />
        <span class="align-middle">Couldn't write to Excel</span>
        <span v-if="errorMessage" class="ms-1.5 text-[11px] text-red-600 truncate max-w-[320px]">{{ errorMessage }}</span>
      </span>
    </div>

    <Transition name="fade">
      <div v-if="!collapsed && hasDetail" class="mt-2 ms-4 text-xs text-gray-600">
        <div v-if="title" class="mb-1 text-gray-700"><span class="text-gray-400">Title:</span> {{ title }}</div>
        <div v-if="columnNames.length" class="mb-1">
          <span class="text-gray-400">Columns:</span>
          <span class="ms-1">{{ columnNames.join(', ') }}</span>
        </div>
        <div v-if="rowPreview.length" class="mt-2">
          <div class="text-[10px] text-gray-400 mb-0.5">Preview ({{ rowPreview.length }} of {{ rowCount }} rows)</div>
          <pre class="code-block text-[11px] text-gray-700 bg-gray-50 border border-gray-100 rounded p-2 overflow-x-auto"><code>{{ previewText }}</code></pre>
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
