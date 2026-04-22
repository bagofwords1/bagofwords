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
        class="w-3 h-3 me-1 text-gray-400 rtl-flip"
      />
      <span v-if="isRunning" class="tool-shimmer flex items-center text-gray-500">
        <Icon name="heroicons-table-cells" class="w-3 h-3 me-1 text-gray-400" />
        Reading {{ rangeLabel }}…
      </span>
      <span v-else-if="succeeded" class="text-gray-700 flex items-center">
        <Icon name="heroicons-check" class="w-3 h-3 me-1 text-green-500" />
        <span class="align-middle">Read {{ rangeLabel }}</span>
        <span v-if="truncated" class="ms-1.5 text-[10px] px-1 py-0.5 rounded bg-amber-50 text-amber-700">truncated</span>
      </span>
      <span v-else class="text-red-500 flex items-center">
        <Icon name="heroicons-exclamation-circle" class="w-3 h-3 me-1" />
        <span class="align-middle">{{ $t('tools.readExcelRange.readFailed') }}</span>
        <span v-if="errorMessage" class="ms-1.5 text-[11px] text-red-600 truncate max-w-[320px]">{{ errorMessage }}</span>
      </span>
    </div>

    <Transition name="fade">
      <div v-if="!collapsed && hasDetail" class="mt-2 ms-4 space-y-2 text-xs text-gray-600">
        <div v-for="(r, idx) in ranges" :key="idx">
          <div class="text-[10px] text-gray-400 mb-0.5">
            {{ r.address }} <span v-if="r.row_count != null && r.col_count != null">· {{ r.row_count }}×{{ r.col_count }}</span>
          </div>
          <pre v-if="r.preview" class="code-block text-[11px] text-gray-700 bg-gray-50 border border-gray-100 rounded p-2 overflow-x-auto"><code>{{ r.preview }}</code></pre>
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

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const status = computed<string>(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})
const aj = computed<any>(() => props.toolExecution?.arguments_json || {})

const sheetName = computed<string>(() => aj.value?.sheet_name || '')
const requestedRanges = computed<string[]>(() => Array.isArray(aj.value?.ranges) ? aj.value.ranges : [])

const isRunning = computed<boolean>(() => status.value === 'running' || (!('success' in rj.value) && status.value !== 'success' && status.value !== 'error' && status.value !== 'stopped'))
const succeeded = computed<boolean>(() => !isRunning.value && rj.value?.success === true)
const errorMessage = computed<string>(() => rj.value?.error || props.toolExecution?.result_summary || '')
const truncated = computed<boolean>(() => !!rj.value?.truncated)

const rangeLabel = computed<string>(() => {
  const list = requestedRanges.value
  if (!sheetName.value) return 'Excel range'
  if (!list.length) return sheetName.value
  if (list.length === 1) return `${sheetName.value}!${list[0]}`
  return `${sheetName.value}!${list[0]} +${list.length - 1}`
})

const resultRanges = computed<any[]>(() => Array.isArray(rj.value?.ranges) ? rj.value.ranges : [])

function previewFor(values: any[][] | undefined, maxRows = 6, maxCols = 8): string {
  if (!Array.isArray(values) || !values.length) return ''
  const rows = values.slice(0, maxRows).map(row => {
    const cells = (Array.isArray(row) ? row : []).slice(0, maxCols).map(v => {
      if (v === null || v === undefined) return ''
      const s = typeof v === 'string' ? v : JSON.stringify(v)
      return s.length > 40 ? s.slice(0, 37) + '…' : s
    })
    const more = (Array.isArray(row) && row.length > maxCols) ? ', …' : ''
    return cells.join('\t') + more
  })
  const more = values.length > maxRows ? '\n…' : ''
  return rows.join('\n') + more
}

const ranges = computed(() => resultRanges.value.map(r => ({
  address: r?.address || '',
  row_count: r?.row_count,
  col_count: r?.col_count,
  preview: previewFor(r?.values),
})))

const hasDetail = computed<boolean>(() => ranges.value.some(r => !!r.preview))

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
