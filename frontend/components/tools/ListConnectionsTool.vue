<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
          <span>Listing connections{{ filterLabel ? ` · ${filterLabel}` : '' }}…</span>
        </span>
        <span v-else class="text-gray-700 flex items-center">
          <Icon name="heroicons-link" class="w-3 h-3 me-1 text-gray-400" />
          <span class="align-middle">
            Found <span class="font-medium">{{ total }}</span> connection{{ total === 1 ? '' : 's' }}
            <span v-if="filterLabel" class="text-gray-500"> · {{ filterLabel }}</span>
          </span>
        </span>
      </div>
    </Transition>

    <Transition name="fade" appear>
      <div v-if="connections.length" class="text-xs text-gray-600">
        <ul class="ms-1 space-y-1 leading-snug">
          <li v-for="(conn, idx) in connections" :key="conn.id || idx">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon
                :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
                class="w-3 h-3 text-gray-400 me-1 rtl-flip"
              />
              <DataSourceIcon :type="conn.type || 'resource'" class="h-2 me-1" />
              <span class="font-medium text-gray-700 truncate">{{ conn.name }}</span>
              <span class="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500 ms-2 flex-shrink-0 uppercase tracking-wide">
                {{ conn.type }}
              </span>
              <span
                v-if="!conn.is_indexed"
                class="text-[9px] px-1 py-0.5 rounded bg-yellow-50 text-yellow-700 ms-1 flex-shrink-0"
                title="No tables/tools indexed yet"
              >indexing</span>
              <span class="text-[10px] text-gray-400 ms-auto flex-shrink-0">
                <template v-if="isToolProvider(conn)">
                  {{ conn.tool_count }} tools
                </template>
                <template v-else>
                  {{ conn.table_count }} tables
                </template>
              </span>
            </div>
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-1 space-y-1">
                <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-400">
                  <span class="inline-flex items-center gap-1">
                    <Icon name="heroicons-shield-check" class="w-3 h-3" />
                    {{ conn.auth_policy === 'user_required' ? 'user creds' : 'system creds' }}
                  </span>
                  <span class="inline-flex items-center gap-1">
                    <Icon name="heroicons-table-cells" class="w-3 h-3" />
                    {{ conn.table_count }} tables
                  </span>
                  <span v-if="isToolProvider(conn)" class="inline-flex items-center gap-1">
                    <Icon name="heroicons-wrench-screwdriver" class="w-3 h-3" />
                    {{ conn.tool_count }} tools
                  </span>
                  <span v-if="conn.last_synced_at" class="inline-flex items-center gap-1">
                    <Icon name="heroicons-clock" class="w-3 h-3" />
                    synced {{ shortDate(conn.last_synced_at) }}
                  </span>
                </div>
                <div v-if="conn.agent_names && conn.agent_names.length" class="text-gray-500">
                  <span class="text-[10px] text-gray-400 me-1">used by:</span>
                  <span
                    v-for="name in conn.agent_names"
                    :key="name"
                    class="inline-block text-[10px] px-1 py-0.5 rounded bg-blue-50 text-blue-700 me-1 mb-0.5"
                  >{{ name }}</span>
                </div>
                <div v-else class="text-[10px] text-gray-400">no agents linked</div>
              </div>
            </Transition>
          </li>
        </ul>
        <div v-if="total > connections.length" class="text-[10px] text-gray-400 mt-1 ms-2">
          showing {{ connections.length }} of {{ total }}
        </div>
      </div>
    </Transition>

    <div
      v-if="status !== 'running' && !connections.length"
      class="text-xs text-gray-400 ms-1"
    >
      No connections matched.
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface ToolExecution {
  status: string
  result_json?: any
  arguments_json?: any
}
const props = defineProps<{ toolExecution: ToolExecution }>()

const TOOL_PROVIDER_TYPES = new Set(['mcp', 'custom_api'])

const status = computed<string>(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})

const connections = computed<any[]>(() => Array.isArray(result.value.connections) ? result.value.connections : [])
const total = computed<number>(() => Number(result.value.total ?? connections.value.length))

const filterLabel = computed<string>(() => {
  const parts: string[] = []
  if (args.value.name_search) parts.push(`"${args.value.name_search}"`)
  if (args.value.type) parts.push(`type=${args.value.type}`)
  if (args.value.only_tool_providers) parts.push('tools only')
  return parts.join(' · ')
})

const expandedItems = ref<Set<number>>(new Set())
function toggleItem(i: number) {
  if (expandedItems.value.has(i)) expandedItems.value.delete(i)
  else expandedItems.value.add(i)
}
function isExpanded(i: number) { return expandedItems.value.has(i) }

function isToolProvider(conn: any): boolean {
  return TOOL_PROVIDER_TYPES.has(conn?.type)
}

function shortDate(iso: string): string {
  try {
    const d = new Date(iso)
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  } catch { return '' }
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
