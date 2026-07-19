<template>
  <div class="mt-1">
    <!-- Status header (click to expand/collapse results) -->
    <Transition name="fade" appear>
      <div
        class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400"
        :class="agents.length ? 'cursor-pointer' : ''"
        @click="agents.length && toggleCollapsed()"
        :aria-expanded="!isCollapsed"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ queryLabel ? `Searching agents for ${queryLabel}` : 'Searching agents' }}</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center min-w-0">
          <Icon
            v-if="agents.length"
            :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'"
            class="w-3 h-3 text-gray-400 me-1 flex-shrink-0 rtl-flip"
          />
          <span class="align-middle flex-shrink-0">{{ total === 1 ? 'Found 1 agent' : `Found ${total} agents` }}</span>
          <!-- inline icon preview of the matched agents -->
          <span v-if="agents.length" class="ms-2 flex items-center gap-0.5 min-w-0 overflow-hidden">
            <DataSourceIcon
              v-for="a in agents.slice(0, 8)"
              :key="a.id"
              :type="a.type"
              :connector-key="a.connector_key"
              :icon="a.icon"
              class="w-3.5 h-3.5 flex-shrink-0"
            />
            <span v-if="agents.length > 8" class="ms-0.5 text-[10px] text-gray-400 flex-shrink-0">+{{ agents.length - 8 }}</span>
          </span>
        </span>
      </div>
    </Transition>

    <!-- Results list (collapsed by default; click header to toggle) -->
    <Transition name="fade">
      <div v-if="!isCollapsed && agents.length" class="text-xs text-gray-600 dark:text-gray-400">
        <ul class="ms-1 space-y-0.5 leading-snug">
          <li
            v-for="(a, idx) in agents"
            :key="a.id || idx"
            class="flex items-center py-1 px-1 rounded"
          >
            <DataSourceIcon
              :type="a.type"
              :connector-key="a.connector_key"
              :icon="a.icon"
              class="w-3.5 h-3.5 me-1.5 flex-shrink-0"
            />
            <span class="font-medium text-gray-700 dark:text-gray-300 truncate">{{ a.name }}</span>
            <span v-if="a.description" class="ms-1.5 text-gray-500 dark:text-gray-400 truncate hidden sm:inline">
              — {{ a.description }}
            </span>
            <span
              v-if="a.focused"
              class="ms-1.5 text-[9px] px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-950 text-blue-600 flex-shrink-0"
            >focused</span>
            <span
              v-else-if="a.status && a.status !== 'published'"
              class="ms-1.5 text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0"
            >{{ a.status }}</span>
          </li>
        </ul>
      </div>
    </Transition>

    <!-- Empty state -->
    <div v-if="status !== 'running' && !agents.length" class="text-xs text-gray-400 ms-1">
      No agents matched.
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

const isCollapsed = ref(true)
function toggleCollapsed() { isCollapsed.value = !isCollapsed.value }

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed<string>(() => props.toolExecution?.status || '')

const queryLabel = computed<string>(() => {
  const q = (props.toolExecution as any)?.arguments_json?.query
  if (Array.isArray(q)) return q.filter(Boolean).map((s: string) => `"${s}"`).join(', ')
  if (typeof q === 'string' && q) return `"${q}"`
  return ''
})

const agents = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.agents) ? rj.agents : []
})

const total = computed<number>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return typeof rj.total === 'number' ? rj.total : agents.value.length
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
