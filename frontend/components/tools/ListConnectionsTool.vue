<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-circle-stack" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ $t('tools.listConnections.listing') }}</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-circle-stack" class="w-3 h-3 me-1 text-gray-400" />
          <span class="align-middle">{{ $t('tools.listConnections.listed') }}</span>
          <span class="ms-1.5 text-[10px] text-gray-400">· {{ $t('tools.listConnections.count', { count: total }) }}</span>
        </span>
      </div>
    </Transition>

    <!-- Connections list -->
    <Transition name="fade" appear>
      <div v-if="connections.length" class="text-xs text-gray-600 dark:text-gray-400">
        <ul class="ms-1 space-y-1 leading-snug">
          <li v-for="(item, idx) in connections" :key="item.id || idx">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 me-1 rtl-flip" />
              <Icon :name="shapeIcon(item.data_shape)" class="w-3 h-3 me-1 text-blue-400 flex-shrink-0" />
              <div class="font-medium text-gray-700 dark:text-gray-300 truncate">{{ item.name }}</div>
              <span class="ms-1.5 text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">{{ item.type }}</span>
              <span class="ms-1 text-[9px] px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-400 flex-shrink-0">
                {{ countLabel(item) }}
              </span>
            </div>
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-2 space-y-0.5">
                <div v-if="item.schemas && item.schemas.length" class="text-[11px]">
                  <span class="text-gray-400">{{ $t('tools.listConnections.schemas') }}:</span>
                  {{ item.schemas.join(', ') }}
                </div>
                <div v-if="item.linked_agents && item.linked_agents.length" class="text-[11px]">
                  <span class="text-gray-400">{{ $t('tools.listConnections.agents') }}:</span>
                  {{ item.linked_agents.join(', ') }}
                </div>
                <div class="text-[10px] text-gray-400 font-mono">{{ item.id }}</div>
              </div>
            </Transition>
          </li>
        </ul>
      </div>
    </Transition>

    <!-- Empty state -->
    <div v-if="status !== 'running' && !connections.length" class="text-xs text-gray-400 ms-1">
      {{ $t('tools.listConnections.empty') }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
const { t } = useI18n()

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
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer { 0% { background-position: 0% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
