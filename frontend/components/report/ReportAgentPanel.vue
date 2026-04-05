<template>
  <div class="h-full flex flex-col overflow-hidden">
    <!-- Agent selector dropdown -->
    <div class="px-4 pt-4 pb-2 flex-shrink-0">
      <div v-if="agents.length === 0" class="text-xs text-gray-400 italic text-center py-4">
        No agents attached to this report
      </div>
      <div v-else-if="agents.length === 1" class="flex items-center gap-2">
        <DataSourceIcon :type="agents[0].type" class="h-4 flex-shrink-0" />
        <span class="text-sm font-semibold text-gray-900 truncate">{{ agents[0].name }}</span>
        <a :href="`/data/${agents[0].id}`" class="ml-auto text-[11px] text-indigo-600 hover:text-indigo-700 hover:underline flex-shrink-0">
          Open →
        </a>
      </div>
      <div v-else class="relative" ref="dropdownRef">
        <button
          @click="dropdownOpen = !dropdownOpen"
          class="w-full flex items-center gap-2 px-3 py-2 border border-gray-200 rounded-lg text-sm hover:bg-gray-50 transition-colors"
        >
          <DataSourceIcon v-if="selectedAgent" :type="selectedAgent.type" class="h-4 flex-shrink-0" />
          <span class="truncate flex-1 text-left font-medium text-gray-900">
            {{ selectedAgent?.name || 'Select agent' }}
          </span>
          <Icon name="heroicons:chevron-down" class="w-4 h-4 text-gray-400 flex-shrink-0 transition-transform" :class="{ 'rotate-180': dropdownOpen }" />
        </button>
        <Transition
          enter-active-class="transition duration-100 ease-out"
          enter-from-class="opacity-0 scale-95"
          enter-to-class="opacity-100 scale-100"
          leave-active-class="transition duration-75 ease-in"
          leave-from-class="opacity-100 scale-100"
          leave-to-class="opacity-0 scale-95"
        >
          <div v-if="dropdownOpen" class="absolute z-10 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden">
            <button
              v-for="agent in agents"
              :key="agent.id"
              @click="selectAgent(agent.id)"
              class="w-full flex items-center gap-2 px-3 py-2 text-xs hover:bg-gray-50 transition-colors"
              :class="selectedAgentId === agent.id ? 'bg-indigo-50 text-indigo-700' : 'text-gray-700'"
            >
              <DataSourceIcon :type="agent.type" class="h-3.5 flex-shrink-0" />
              <span class="truncate flex-1 text-left font-medium">{{ agent.name }}</span>
              <Icon v-if="selectedAgentId === agent.id" name="heroicons:check" class="w-3.5 h-3.5 text-indigo-600 flex-shrink-0" />
            </button>
          </div>
        </Transition>
      </div>
    </div>

    <!-- Tabs -->
    <div v-if="selectedAgent" class="border-b border-gray-200 px-4 flex-shrink-0">
      <nav class="-mb-px flex space-x-4">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          @click="activeTab = tab.key"
          :class="[
            activeTab === tab.key
              ? 'border-indigo-500 text-indigo-600'
              : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700',
            'whitespace-nowrap border-b-2 py-2 text-xs font-medium'
          ]"
        >
          {{ tab.label }}
          <span v-if="tab.count > 0" class="ml-1 text-[10px] text-gray-400">({{ tab.count }})</span>
        </button>
      </nav>
    </div>

    <!-- Tab content -->
    <div v-if="selectedAgent" class="flex-1 min-h-0 overflow-y-auto p-4 bg-white">
      <!-- Tables tab — uses TablesSelector component -->
      <div v-if="activeTab === 'tables'" class="h-full">
        <TablesSelector
          :key="selectedAgentId"
          :ds-id="selectedAgentId!"
          schema="full"
          :can-update="canUpdateDataSource"
          :show-refresh="false"
          :show-save="canUpdateDataSource"
          save-label="Save"
          :show-stats="canUpdateDataSource"
          max-height="calc(100vh - 280px)"
        />
      </div>

      <!-- Instructions tab -->
      <div v-else-if="activeTab === 'instructions'">
        <div v-if="loading" class="flex items-center justify-center py-10">
          <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
        </div>
        <template v-else>
          <div v-if="instructionsError" class="text-xs text-gray-500">{{ instructionsError }}</div>
          <div v-else-if="instructions.length === 0" class="text-xs text-gray-400 italic py-6 text-center">No instructions found</div>
          <div v-else class="border border-gray-200 rounded-lg overflow-hidden">
            <a
              v-for="inst in instructions"
              :key="inst.id"
              :href="`/instructions?search=${encodeURIComponent(inst.title || '')}`"
              class="w-full px-3 py-2 text-left text-xs flex items-start gap-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0 block"
            >
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-1.5">
                  <span class="truncate text-gray-800 font-medium">{{ inst.title || 'Untitled' }}</span>
                  <span
                    v-if="!inst.data_sources?.length"
                    class="px-1 py-0.5 text-[9px] rounded bg-purple-50 text-purple-600 flex-shrink-0"
                  >Global</span>
                </div>
                <div class="text-[11px] text-gray-400 truncate mt-0.5">
                  {{ inst.category || 'general' }} · {{ inst.source_type || 'user' }}
                </div>
              </div>
            </a>
          </div>
        </template>
      </div>

      <!-- Queries tab -->
      <div v-else-if="activeTab === 'queries'">
        <div v-if="loading" class="flex items-center justify-center py-10">
          <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
        </div>
        <template v-else>
          <div v-if="queriesError" class="text-xs text-gray-500">{{ queriesError }}</div>
          <div v-else-if="queries.length === 0" class="text-xs text-gray-400 italic py-6 text-center">No saved queries found</div>
          <div v-else class="border border-gray-200 rounded-lg overflow-hidden">
            <a
              v-for="entity in queries"
              :key="entity.id"
              :href="`/queries/${entity.id}`"
              class="w-full px-3 py-2 text-left text-xs flex items-start gap-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0 block"
            >
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-1.5">
                  <span
                    class="px-1 py-0.5 text-[9px] rounded border flex-shrink-0"
                    :class="entity.type === 'metric' ? 'text-emerald-700 border-emerald-200 bg-emerald-50' : 'text-blue-700 border-blue-200 bg-blue-50'"
                  >{{ (entity.type || 'entity').toUpperCase() }}</span>
                  <span class="truncate text-gray-800 font-medium">{{ entity.title || entity.slug }}</span>
                </div>
                <div v-if="entity.description" class="text-[11px] text-gray-400 truncate mt-0.5">
                  {{ entity.description }}
                </div>
              </div>
            </a>
          </div>
        </template>
      </div>

      <!-- Evals tab -->
      <div v-else-if="activeTab === 'evals'">
        <div v-if="loading" class="flex items-center justify-center py-10">
          <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
        </div>
        <template v-else>
          <div v-if="evalsError" class="text-xs text-gray-500">{{ evalsError }}</div>
          <div v-else-if="evals.length === 0" class="text-xs text-gray-400 italic py-6 text-center">No evals found for this agent</div>
          <div v-else class="border border-gray-200 rounded-lg overflow-hidden">
            <a
              v-for="tc in evals"
              :key="tc.id"
              :href="`/evals`"
              class="w-full px-3 py-2 text-left text-xs flex items-start gap-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0 block"
            >
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-1.5">
                  <span class="px-1 py-0.5 text-[9px] rounded border flex-shrink-0 text-amber-700 border-amber-200 bg-amber-50">
                    {{ tc.suite_name || 'EVAL' }}
                  </span>
                  <span class="truncate text-gray-800 font-medium">{{ tc.name || promptPreview(tc.prompt_json) }}</span>
                </div>
                <div v-if="tc.expectations_json?.length" class="text-[11px] text-gray-400 truncate mt-0.5">
                  {{ tc.expectations_json.length }} expectation{{ tc.expectations_json.length > 1 ? 's' : '' }}
                </div>
              </div>
            </a>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import TablesSelector from '~/components/datasources/TablesSelector.vue'

const props = defineProps<{
  agents: Array<{ id: string; name: string; type?: string; connections?: any[] }>
}>()

// Permissions
const canUpdateDataSource = computed(() => useCan('update_data_source'))

// Dropdown state
const dropdownOpen = ref(false)
const dropdownRef = ref<HTMLElement | null>(null)
const selectedAgentId = ref<string | null>(null)

// Tab state
const activeTab = ref<'tables' | 'instructions' | 'queries' | 'evals'>('tables')

// Data caches (keyed by agent id)
const instructionsCache = ref<Record<string, any[]>>({})
const queriesCache = ref<Record<string, any[]>>({})
const evalsCache = ref<Record<string, any[]>>({})

// Loading & error state
const loading = ref(false)
const instructionsError = ref<string | null>(null)
const queriesError = ref<string | null>(null)
const evalsError = ref<string | null>(null)

// Auto-select first agent
watch(() => props.agents, (agents) => {
  if (agents.length > 0 && !selectedAgentId.value) {
    selectedAgentId.value = agents[0].id
  }
}, { immediate: true })

const selectedAgent = computed(() => {
  return props.agents.find(a => a.id === selectedAgentId.value) || null
})

// Tab definitions with counts (tables count managed by TablesSelector internally)
const tabs = computed(() => [
  { key: 'tables' as const, label: 'Tables', count: 0 },
  { key: 'instructions' as const, label: 'Instructions', count: instructions.value.length },
  { key: 'queries' as const, label: 'Queries', count: queries.value.length },
  { key: 'evals' as const, label: 'Evals', count: evals.value.length },
])

const instructions = computed(() => selectedAgentId.value ? (instructionsCache.value[selectedAgentId.value] || []) : [])
const queries = computed(() => selectedAgentId.value ? (queriesCache.value[selectedAgentId.value] || []) : [])
const evals = computed(() => selectedAgentId.value ? (evalsCache.value[selectedAgentId.value] || []) : [])

function selectAgent(agentId: string) {
  selectedAgentId.value = agentId
  dropdownOpen.value = false
}

function promptPreview(promptJson: any): string {
  if (!promptJson) return 'Untitled'
  if (typeof promptJson === 'string') return promptJson.slice(0, 60)
  if (Array.isArray(promptJson) && promptJson.length > 0) {
    const first = promptJson[0]
    const content = first?.content || first?.text || ''
    return typeof content === 'string' ? content.slice(0, 60) : 'Untitled'
  }
  return 'Untitled'
}

// Close dropdown on outside click
function onClickOutside(e: MouseEvent) {
  if (dropdownRef.value && !dropdownRef.value.contains(e.target as Node)) {
    dropdownOpen.value = false
  }
}

onMounted(() => {
  document.addEventListener('click', onClickOutside)
})

onUnmounted(() => {
  document.removeEventListener('click', onClickOutside)
})

// Fetch data for active tab when agent or tab changes
async function fetchTabData(agentId: string, tab: string) {
  // Tables tab is handled by TablesSelector component — no manual fetch needed

  if (tab === 'instructions' && !instructionsCache.value[agentId]) {
    loading.value = true
    instructionsError.value = null
    try {
      const { data, error } = await useMyFetch('/api/instructions', {
        method: 'GET',
        query: { data_source_ids: agentId, include_global: true, limit: 50, include_own: true, include_drafts: false }
      })
      if (error?.value) { instructionsError.value = 'Failed to load instructions'; return }
      const payload: any = (data as any)?.value
      instructionsCache.value[agentId] = payload?.items || payload || []
    } catch { instructionsError.value = 'Failed to load instructions' }
    finally { loading.value = false }
  }

  if (tab === 'queries' && !queriesCache.value[agentId]) {
    loading.value = true
    queriesError.value = null
    try {
      const { data, error } = await useMyFetch('/api/entities', {
        method: 'GET',
        query: { data_source_ids: agentId }
      })
      if (error?.value) { queriesError.value = 'Failed to load queries'; return }
      const payload: any = (data as any)?.value
      const entities = Array.isArray(payload) ? payload : []
      queriesCache.value[agentId] = entities.filter((e: any) => e.status === 'published' && e.global_status === 'approved')
    } catch { queriesError.value = 'Failed to load queries' }
    finally { loading.value = false }
  }

  if (tab === 'evals' && !evalsCache.value[agentId]) {
    loading.value = true
    evalsError.value = null
    try {
      const { data, error } = await useMyFetch('/api/tests/cases', {
        method: 'GET',
        query: { data_source_id: agentId, limit: 50 }
      })
      if (error?.value) { evalsError.value = 'Failed to load evals'; return }
      const payload: any = (data as any)?.value
      const cases = Array.isArray(payload) ? payload : []
      evalsCache.value[agentId] = cases.filter((c: any) => {
        const dsIds = c.data_source_ids_json || []
        return dsIds.includes(agentId)
      })
    } catch { evalsError.value = 'Failed to load evals' }
    finally { loading.value = false }
  }
}

watch([selectedAgentId, activeTab], ([agentId, tab]) => {
  if (agentId && tab) {
    fetchTabData(agentId, tab)
  }
}, { immediate: true })

// Reset when agent changes
watch(selectedAgentId, () => {
  activeTab.value = 'tables'
  instructionsError.value = null
  queriesError.value = null
  evalsError.value = null
})
</script>
