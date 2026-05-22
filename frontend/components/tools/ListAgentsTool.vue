<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
          <span>Listing agents{{ filterLabel ? ` · ${filterLabel}` : '' }}…</span>
        </span>
        <span v-else class="text-gray-700 flex items-center">
          <Icon name="heroicons-cube" class="w-3 h-3 me-1 text-gray-400" />
          <span class="align-middle">
            Found <span class="font-medium">{{ total }}</span> agent{{ total === 1 ? '' : 's' }}
            <span v-if="filterLabel" class="text-gray-500"> · {{ filterLabel }}</span>
          </span>
        </span>
      </div>
    </Transition>

    <!-- Agent list -->
    <Transition name="fade" appear>
      <div v-if="agents.length" class="text-xs text-gray-600">
        <ul class="ms-1 space-y-1 leading-snug">
          <li v-for="(agent, idx) in agents" :key="agent.id || idx">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon
                :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
                class="w-3 h-3 text-gray-400 me-1 rtl-flip"
              />
              <DataSourceIcon :type="agent.type || 'resource'" class="h-2 me-1" />
              <span class="font-medium text-gray-700 truncate">{{ agent.name }}</span>
              <span
                v-if="!agent.is_public"
                class="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500 ms-2 flex-shrink-0"
              >private</span>
              <span class="text-[10px] text-gray-400 ms-auto flex-shrink-0">
                {{ agent.connection_count }} conn · {{ agent.table_count }} tables
              </span>
            </div>
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-1 space-y-1">
                <p v-if="agent.description" class="text-gray-500">{{ agent.description }}</p>
                <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-400">
                  <span v-if="agent.type" class="inline-flex items-center gap-1">
                    <Icon name="heroicons-circle-stack" class="w-3 h-3" />
                    {{ agent.type }}
                  </span>
                  <span class="inline-flex items-center gap-1">
                    <Icon name="heroicons-link" class="w-3 h-3" />
                    {{ agent.connection_count }} connection{{ agent.connection_count === 1 ? '' : 's' }}
                  </span>
                  <span class="inline-flex items-center gap-1">
                    <Icon name="heroicons-table-cells" class="w-3 h-3" />
                    {{ agent.table_count }} table{{ agent.table_count === 1 ? '' : 's' }}
                  </span>
                  <span v-if="agent.created_at" class="inline-flex items-center gap-1">
                    <Icon name="heroicons-clock" class="w-3 h-3" />
                    {{ shortDate(agent.created_at) }}
                  </span>
                </div>
              </div>
            </Transition>
          </li>
        </ul>
        <div v-if="total > agents.length" class="text-[10px] text-gray-400 mt-1 ms-2">
          showing {{ agents.length }} of {{ total }}
        </div>
      </div>
    </Transition>

    <div
      v-if="status !== 'running' && !agents.length"
      class="text-xs text-gray-400 ms-1"
    >
      No agents matched.
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

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
const result = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})

const agents = computed<any[]>(() => Array.isArray(result.value.agents) ? result.value.agents : [])
const total = computed<number>(() => Number(result.value.total ?? agents.value.length))

const filterLabel = computed<string>(() => {
  const parts: string[] = []
  if (args.value.name_search) parts.push(`"${args.value.name_search}"`)
  if (args.value.type) parts.push(`type=${args.value.type}`)
  return parts.join(' · ')
})

const expandedItems = ref<Set<number>>(new Set())
function toggleItem(i: number) {
  if (expandedItems.value.has(i)) expandedItems.value.delete(i)
  else expandedItems.value.add(i)
}
function isExpanded(i: number) { return expandedItems.value.has(i) }

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
