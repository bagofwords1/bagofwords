<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-magnifying-glass" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">
      <template v-if="modelTitle">{{ modelTitle }}…</template>
      <template v-else>{{ t('tools.grepFiles.grepping', { pattern: patternLabel }) }}</template></template>
      <template v-else>{{ modelTitle || t('tools.grepFiles.grepped', { pattern: patternLabel }) }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'"></template>
      <template v-else><span v-if="rj.success" class="ms-2 text-ink-muted">
            ({{ matchSummary }})
          </span><span v-if="rj.truncated" class="ms-2 text-chat-3 text-yellow-600">{{ t('tools.grepFiles.truncated') }}</span><span
            v-if="rj.stop_reason && rj.stop_reason !== 'complete'"
            class="ms-2 text-chat-3 px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400"
          >{{ t('tools.grepFiles.stopped', { reason: rj.stop_reason }) }}</span></template></div>

    <Transition name="fade" appear>
      <div v-if="toolHeaderExpanded && status !== 'running'" class="text-chat-2 text-gray-600 dark:text-gray-400">
        <ul v-if="matches.length" class="ms-1 space-y-1 leading-snug">
          <li v-for="(m, idx) in matches.slice(0, 10)" :key="idx">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-ink-muted me-1 rtl-flip" />
              <span class="font-medium text-gray-700 dark:text-gray-300 truncate shrink-0">{{ m.path || m.file_id }}<span class="text-ink-muted">:{{ m.line_no }}</span></span>
              <span class="ms-2 truncate text-gray-500 dark:text-gray-400 font-mono text-chat-2">{{ m.line }}</span>
            </div>
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-1">
                <pre class="text-chat-2 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded p-2 max-h-48 overflow-auto whitespace-pre-wrap">{{ contextBlock(m) }}</pre>
                <div v-if="m.line_truncated" class="mt-0.5 text-chat-2 text-yellow-600">{{ t('tools.grepFiles.lineClipped', { n: 500 }) }}</div>
              </div>
            </Transition>
          </li>
          <li v-if="matches.length > 10" class="ps-1 text-chat-3 text-ink-muted">{{ t('tools.grepFiles.moreShown', { count: matches.length - 10 }) }}</li>
        </ul>
        <div v-if="skipped.length" class="mt-1 ms-1 text-chat-2 text-ink-muted">
          {{ skipSummary }}
        </div>
        <ToolCallParams :params="toolExecution?.arguments_json" />
      </div>
    </Transition>

    <div v-if="status !== 'running' && !matches.length && errorMessage" class="text-chat-2 text-amber-600 mt-1">{{ errorMessage }}</div>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed, ref } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
import ToolCallParams from '~/components/tools/ToolCallParams.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useToolConnectionIcon, FILE_SOURCE_TYPES } from '~/composables/useToolConnectionIcon'

const { t } = useResponseI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution; dataSources?: any[] }>()

const connIcon = useToolConnectionIcon(
  () => props.toolExecution,
  () => props.dataSources,
  { connectionTypes: FILE_SOURCE_TYPES },
)

const status = computed(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})

const modelTitle = computed<string>(() => {
  const t = props.toolExecution?.arguments_json?.title
  return typeof t === 'string' && t.trim() ? t.trim() : ''
})

const patternLabel = computed(() => {
  const p = props.toolExecution?.arguments_json?.pattern ?? rj.value.pattern
  return typeof p === 'string' ? `/${p}/` : 'pattern'
})

const matches = computed<any[]>(() => Array.isArray(rj.value.matches) ? rj.value.matches : [])
const skipped = computed<any[]>(() => Array.isArray(rj.value.files_skipped) ? rj.value.files_skipped : [])
const skipReasons = computed(() => {
  const counts: Record<string, number> = {}
  for (const s of skipped.value) counts[s.reason] = (counts[s.reason] || 0) + 1
  return Object.entries(counts).map(([r, n]) => `${n} ${r}`).join(', ')
})
const matchSummary = computed(() => {
  const total = rj.value.total_matches ?? 0
  const key = total === 1 ? 'tools.grepFiles.matchSingular' : 'tools.grepFiles.matchPlural'
  return t(key, {
    count: total,
    inFiles: rj.value.files_with_matches ?? 0,
    scanned: rj.value.files_scanned ?? 0,
  })
})
const skipSummary = computed(() => {
  const n = skipped.value.length
  const key = n === 1 ? 'tools.grepFiles.filesSkippedSingular' : 'tools.grepFiles.filesSkippedPlural'
  return t(key, { count: n, reasons: skipReasons.value })
})
const errorMessage = computed(() => rj.value.error || '')

const expanded = ref(false)

const expandedItems = ref<Set<number>>(new Set())
function toggleItem(i: number) {
  if (expandedItems.value.has(i)) expandedItems.value.delete(i)
  else expandedItems.value.add(i)
}
function isExpanded(i: number) { return expandedItems.value.has(i) }

function contextBlock(m: any): string {
  const before = Array.isArray(m.before) ? m.before : []
  const after = Array.isArray(m.after) ? m.after : []
  const start = m.line_no - before.length
  const rows: string[] = []
  before.forEach((l: string, i: number) => rows.push(`${start + i}  ${l}`))
  rows.push(`${m.line_no}> ${m.line}`)
  after.forEach((l: string, i: number) => rows.push(`${m.line_no + 1 + i}  ${l}`))
  return rows.join('\n')
}
</script>

<style scoped>
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
