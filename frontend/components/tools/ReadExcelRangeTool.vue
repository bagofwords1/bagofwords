<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-table-cells" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="isRunning">
        Reading {{ rangeLabel }}…
      </template>
      <template v-else-if="succeeded">Read {{ rangeLabel }}</template>
      <template v-else>{{ t('tools.readExcelRange.readFailed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="isRunning"></template>
      <template v-else-if="succeeded"><span v-if="truncated" class="ms-1.5 text-chat-3 px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300">truncated</span></template>
      <template v-else><span v-if="errorMessage" class="ms-1.5 text-chat-2 text-red-600 truncate max-w-[320px]">{{ errorMessage }}</span></template></div>

    <Transition name="fade">
      <div v-if="toolHeaderExpanded && hasDetail" class="mt-2 ms-4 space-y-2 text-chat-2 text-gray-600 dark:text-gray-400">
        <div v-for="(r, idx) in ranges" :key="idx">
          <div class="text-chat-3 text-ink-muted mb-0.5">
            {{ r.address }} <span v-if="r.row_count != null && r.col_count != null">· {{ r.row_count }}×{{ r.col_count }}</span>
          </div>
          <pre v-if="r.preview" class="code-block text-chat-2 text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 rounded p-2 overflow-x-auto"><code>{{ r.preview }}</code></pre>
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
