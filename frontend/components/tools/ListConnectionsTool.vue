<template>
  <div class="min-w-0">
    <!-- Status header -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-table-cells" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.listConnections.listing') }}</template>
      <template v-else>{{ t('tools.listConnections.listed') }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'"></template>
      <template v-else><span class="ms-1.5 text-chat-3 text-ink-muted">· {{ t('tools.listConnections.count', { count: total }) }}</span></template></div>

    <!-- Connections list -->
    <Transition name="fade" appear>
      <div v-if="connections.length" class="text-chat-2 text-gray-600 dark:text-gray-400">
        <ul class="ms-1 space-y-1 leading-snug">
          <li v-for="(item, idx) in connections" :key="item.id || idx">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-ink-muted me-1 rtl-flip" />
              <Icon :name="shapeIcon(item.data_shape)" class="w-3 h-3 me-1 text-blue-400 flex-shrink-0" />
              <div class="font-medium text-gray-700 dark:text-gray-300 truncate">{{ item.name }}</div>
              <span class="ms-1.5 text-chat-3 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">{{ item.type }}</span>
              <span class="ms-1 text-chat-3 px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-400 flex-shrink-0">
                {{ countLabel(item) }}
              </span>
            </div>
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-2 space-y-0.5">
                <div v-if="item.schemas && item.schemas.length" class="text-chat-2">
                  <span class="text-ink-muted">{{ t('tools.listConnections.schemas') }}:</span>
                  {{ item.schemas.join(', ') }}
                </div>
                <div v-if="item.linked_agents && item.linked_agents.length" class="text-chat-2">
                  <span class="text-ink-muted">{{ t('tools.listConnections.agents') }}:</span>
                  {{ item.linked_agents.join(', ') }}
                </div>
                <div class="text-chat-3 text-ink-muted font-mono">{{ item.id }}</div>
              </div>
            </Transition>
          </li>
        </ul>
      </div>
    </Transition>

    <!-- Empty state -->
    <div v-if="status !== 'running' && !connections.length" class="text-chat-2 text-ink-muted ms-1">
      {{ t('tools.listConnections.empty') }}
    </div>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed, ref } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
const { t } = useResponseI18n()

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
const result = computed(() => props.toolExecution?.result_json || {})

const connections = computed<any[]>(() => Array.isArray(result.value.connections) ? result.value.connections : [])
const total = computed<number>(() => typeof result.value.total === 'number' ? result.value.total : connections.value.length)

const expandedItems = ref<Set<number>>(new Set())
function toggleItem(index: number) {
  if (expandedItems.value.has(index)) expandedItems.value.delete(index)
  else expandedItems.value.add(index)
}
function isExpanded(index: number): boolean { return expandedItems.value.has(index) }

function shapeIcon(shape: string): string {
  if (shape === 'tools') return 'heroicons-wrench-screwdriver'
  if (shape === 'files') return 'heroicons-folder'
  return 'heroicons-table-cells'
}

function countLabel(item: any): string {
  if (item.data_shape === 'tools') return t('tools.listConnections.toolCount', { count: item.tool_count || 0 })
  if (item.data_shape === 'files') return t('tools.listConnections.fileCount', { count: item.table_count || 0 })
  return t('tools.listConnections.tableCount', { count: item.table_count || 0 })
}
</script>

<style scoped>
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
