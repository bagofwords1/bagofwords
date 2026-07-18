<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="status === 'running'" class="flex items-center">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
          <span class="tool-shimmer">
            <template v-if="modelTitle">{{ modelTitle }}…</template>
            <template v-else>Grepping files for {{ patternLabel }}…</template>
          </span>
        </span>
        <span
          v-else
          class="text-gray-700 dark:text-gray-300 flex items-center flex-wrap"
          :class="matches.length ? 'cursor-pointer' : ''"
          @click="matches.length && (expanded = !expanded)"
          :aria-expanded="matches.length ? expanded : undefined"
        >
          <Icon v-if="matches.length" :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 me-1 text-gray-400 rtl-flip" />
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
          <span class="align-middle">{{ modelTitle || ('Grepped files for ' + patternLabel) }}</span>
          <span v-if="rj.success" class="ms-2 text-gray-400">
            ({{ rj.total_matches ?? 0 }} match{{ (rj.total_matches ?? 0) === 1 ? '' : 'es' }}
            in {{ rj.files_with_matches ?? 0 }}/{{ rj.files_scanned ?? 0 }} files)
          </span>
          <span v-if="rj.truncated" class="ms-2 text-[10px] text-yellow-600">truncated</span>
          <span
            v-if="rj.stop_reason && rj.stop_reason !== 'complete'"
            class="ms-2 text-[10px] px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400"
          >stopped: {{ rj.stop_reason }} · resumable</span>
        </span>
      </div>
    </Transition>

    <Transition name="fade" appear>
      <div v-if="matches.length && expanded" class="text-xs text-gray-600 dark:text-gray-400">
        <ul class="ms-1 space-y-1 leading-snug">
          <li v-for="(m, idx) in matches.slice(0, 10)" :key="idx">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 me-1 rtl-flip" />
              <span class="font-medium text-gray-700 dark:text-gray-300 truncate shrink-0">{{ m.path || m.file_id }}<span class="text-gray-400">:{{ m.line_no }}</span></span>
              <span class="ms-2 truncate text-gray-500 dark:text-gray-400 font-mono text-[11px]">{{ m.line }}</span>
            </div>
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-1">
                <pre class="text-[11px] bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded p-2 max-h-48 overflow-auto whitespace-pre-wrap">{{ contextBlock(m) }}</pre>
                <div v-if="m.line_truncated" class="mt-0.5 text-[10px] text-yellow-600">line clipped at 500 chars</div>
              </div>
            </Transition>
          </li>
          <li v-if="matches.length > 10" class="ps-1 text-[11px] text-gray-400">+{{ matches.length - 10 }} more shown to the agent</li>
        </ul>
      </div>
    </Transition>

    <div v-if="skipped.length" class="mt-1 ms-1 text-[11px] text-gray-400 dark:text-gray-500">
      {{ skipped.length }} file(s) skipped ({{ skipReasons }})
    </div>

    <ToolCallParams v-if="status !== 'running'" :params="toolExecution?.arguments_json" />

    <div v-if="status !== 'running' && !matches.length && errorMessage" class="text-xs text-red-600 mt-1">{{ errorMessage }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'
import ToolCallParams from '~/components/tools/ToolCallParams.vue'

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
