<template>
  <div class="mt-1">
    <!-- Status header -->
    <div
      class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400"
      :class="{ 'cursor-pointer hover:text-gray-700 dark:hover:text-gray-300': reports.length }"
      @click="toggleCollapsed"
    >
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
        <span>Searching reports{{ queryLabel ? ` for ${queryLabel}` : '' }}…</span>
      </span>
      <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
        <Icon
          v-if="reports.length"
          :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'"
          class="w-3 h-3 me-1.5 text-gray-400 rtl-flip"
        />
        <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
        <span class="align-middle">Searched reports{{ queryLabel ? ` for ${queryLabel}` : '' }}</span>
        <span v-if="total > 0" class="ms-1.5 text-[10px] text-gray-400">· {{ total }} {{ total === 1 ? 'match' : 'matches' }}</span>
      </span>
    </div>

    <!-- Results list -->
    <div v-if="reports.length && !isCollapsed" class="text-xs text-gray-600 dark:text-gray-400">
      <ul class="ms-1 space-y-0.5 leading-snug">
        <li v-for="(item, idx) in reports" :key="item.id || idx">
          <div class="flex items-center py-1 px-1 rounded">
            <Icon name="heroicons-document-text" class="w-3 h-3 me-1.5 text-blue-400 flex-shrink-0" />
            <div class="font-medium text-gray-700 dark:text-gray-300 truncate">{{ item.title || 'Untitled' }}</div>
            <span
              v-if="item.status"
              class="ms-1.5 text-[9px] px-1 py-0.5 rounded flex-shrink-0"
              :class="item.status === 'published' ? 'bg-green-50 dark:bg-green-950 text-green-600' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400'"
            >
              {{ item.status }}
            </span>
            <span v-if="item.mode && item.mode !== 'chat'" class="ms-1 text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">{{ item.mode }}</span>
            <Icon v-if="item.has_artifacts" name="heroicons-chart-bar-square" class="ms-1 w-3 h-3 text-gray-400 flex-shrink-0" title="Has artifacts" />
          </div>
        </li>
      </ul>
    </div>

    <!-- Empty state -->
    <div v-if="status !== 'running' && !reports.length" class="text-xs text-gray-400 ms-1">
      No reports found.
    </div>
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

// Results are collapsed by default; the header chevron reveals them.
const isCollapsed = ref(true)
function toggleCollapsed() { isCollapsed.value = !isCollapsed.value }

const queryLabel = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  let q: any = rj.search_query
  if (q == null) q = (props.toolExecution as any)?.arguments_json?.query
  if (typeof q === 'string' && q.trim()) return `"${q.trim()}"`
  return ''
})

const reports = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.reports) ? rj.reports : []
})

const total = computed<number>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return typeof rj.total === 'number' ? rj.total : reports.value.length
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
</style>
