<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-table-cells" class="w-3 h-3 mr-1 text-gray-400" />
          Writing {{ rowLabel }} to Excel…
        </span>
        <span v-else-if="succeeded" class="text-gray-700 flex items-center">
          <Icon name="heroicons-table-cells" class="w-3 h-3 mr-1 text-gray-400" />
          <span class="align-middle">Wrote {{ rowLabel }} to Excel</span>
          <span v-if="columnPreview" class="ml-1.5 text-[10px] text-gray-400">· {{ columnPreview }}</span>
        </span>
        <span v-else class="text-red-500 flex items-center">
          <Icon name="heroicons-exclamation-circle" class="w-3 h-3 mr-1" />
          <span class="align-middle">Couldn't write to Excel{{ errorMessage ? ': ' + errorMessage : '' }}</span>
        </span>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

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
const succeeded = computed<boolean>(() => {
  const rj: any = props.toolExecution?.result_json
  return status.value !== 'running' && (rj?.success === true || props.toolExecution?.status === 'success')
})
const errorMessage = computed<string>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return rj.error_message || rj.error || props.toolExecution?.result_summary || ''
})

const rowCount = computed<number>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  if (typeof rj.row_count === 'number') return rj.row_count
  const rows = rj.excel_action?.data?.widget?.last_step?.data?.rows
  if (Array.isArray(rows)) return rows.length
  const aj: any = props.toolExecution?.arguments_json || {}
  return Array.isArray(aj.rows) ? aj.rows.length : 0
})

const columnCount = computed<number>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  if (typeof rj.column_count === 'number') return rj.column_count
  const cols = rj.excel_action?.data?.widget?.last_step?.data?.columns
  if (Array.isArray(cols)) return cols.length
  const aj: any = props.toolExecution?.arguments_json || {}
  return Array.isArray(aj.columns) ? aj.columns.length : 0
})

const rowLabel = computed<string>(() => {
  const r = rowCount.value
  const c = columnCount.value
  if (!r && !c) return 'data'
  return `${r} row${r === 1 ? '' : 's'} × ${c} col${c === 1 ? '' : 's'}`
})

const columnPreview = computed<string>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  const cols = rj.excel_action?.data?.widget?.last_step?.data?.columns
    || props.toolExecution?.arguments_json?.columns
  if (!Array.isArray(cols) || cols.length === 0) return ''
  const names = cols.slice(0, 3).map((c: any) => c?.headerName || c?.field || '')
    .filter(Boolean)
  if (!names.length) return ''
  const more = cols.length > 3 ? ', …' : ''
  return names.join(', ') + more
})
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
</style>
