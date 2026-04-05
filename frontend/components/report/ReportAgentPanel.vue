<template>
  <div class="h-full flex flex-col overflow-hidden">
    <!-- Agent selector dropdown -->
    <div class="px-4 pt-4 pb-2 flex-shrink-0 bg-gradient-to-b from-indigo-50/40 to-transparent">
      <div v-if="agents.length === 0" class="text-xs text-gray-400 italic text-center py-4">
        No agents attached to this report
      </div>
      <div v-else-if="agents.length === 1" class="flex items-center gap-2">
        <DataSourceIcon :type="agents[0].type || agents[0].connections?.[0]?.type" class="h-5 flex-shrink-0" />
        <span class="text-sm font-semibold text-gray-900 truncate">{{ agents[0].name }}</span>
      </div>
      <div v-else class="relative" ref="dropdownRef">
        <button
          @click="dropdownOpen = !dropdownOpen"
          class="w-full flex items-center gap-2 px-3 py-2 border border-gray-200 rounded-lg text-sm hover:bg-gray-50 transition-colors bg-white/80"
        >
          <DataSourceIcon v-if="selectedAgent" :type="selectedAgent.type || selectedAgent.connections?.[0]?.type" class="h-5 flex-shrink-0" />
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
              <DataSourceIcon :type="agent.type || agent.connections?.[0]?.type" class="h-4 flex-shrink-0" />
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
      <!-- Instructions tab -->
      <div v-if="activeTab === 'instructions'">
        <div v-if="loading" class="flex items-center justify-center py-10">
          <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
        </div>
        <template v-else>
          <!-- Loading instruction from external click -->
          <div v-if="instructionLoading" class="flex items-center justify-center py-10">
            <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
          </div>

          <!-- Instruction detail view -->
          <div v-else-if="selectedInstruction" class="flex flex-col h-full -m-4">
            <button
              @click="selectedInstruction = null"
              class="flex items-center gap-1 px-4 pt-3 pb-2 text-xs text-gray-500 hover:text-gray-700 flex-shrink-0"
            >
              <Icon name="heroicons:chevron-left" class="w-3 h-3" />
              All Instructions
            </button>
            <InstructionGlobalCreateComponent
              :key="selectedInstruction.id"
              :instruction="selectedInstruction"
              @instruction-saved="onInstructionSaved"
              @cancel="selectedInstruction = null"
            />
          </div>

          <!-- Instructions list -->
          <template v-else>
            <div v-if="instructionsError" class="text-xs text-gray-500">{{ instructionsError }}</div>
            <div v-else-if="instructions.length === 0" class="text-xs text-gray-400 italic py-6 text-center">No instructions found</div>
            <template v-else>
              <!-- Filters -->
              <div class="flex flex-col gap-2 mb-3">
                <div class="relative">
                  <Icon name="heroicons:magnifying-glass" class="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-gray-400" />
                  <input
                    v-model="instructionSearch"
                    type="text"
                    placeholder="Search..."
                    class="w-full pl-7 pr-2 py-1.5 text-[11px] border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-300 focus:border-indigo-300"
                  />
                </div>
                <div class="flex items-center gap-2">
                  <!-- Status filter -->
                  <select
                    v-model="instructionStatusFilter"
                    class="text-[11px] border border-gray-200 rounded-md py-1 px-2 text-gray-600 focus:outline-none focus:ring-1 focus:ring-indigo-300 bg-white"
                  >
                    <option value="">All statuses</option>
                    <option v-for="s in instructionStatuses" :key="s" :value="s">{{ helpers.formatStatus(s) }}</option>
                  </select>
                  <!-- Category multi-select -->
                  <div class="relative" ref="categoryDropdownRef">
                    <button
                      @click.stop="categoryDropdownOpen = !categoryDropdownOpen"
                      class="flex items-center gap-1 text-[11px] border border-gray-200 rounded-md py-1 px-2 text-gray-600 hover:bg-gray-50 bg-white"
                    >
                      <span v-if="instructionCategoryFilter.length === 0">All categories</span>
                      <span v-else>{{ instructionCategoryFilter.length }} categor{{ instructionCategoryFilter.length === 1 ? 'y' : 'ies' }}</span>
                      <Icon name="heroicons:chevron-down" class="w-3 h-3 text-gray-400 transition-transform" :class="{ 'rotate-180': categoryDropdownOpen }" />
                    </button>
                    <div v-if="categoryDropdownOpen" class="absolute z-20 mt-1 left-0 bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden min-w-[140px]">
                      <button
                        v-for="cat in instructionCategories"
                        :key="cat"
                        @click.stop="toggleCategoryFilter(cat)"
                        class="w-full flex items-center gap-2 px-2.5 py-1.5 text-[11px] hover:bg-gray-50 transition-colors text-left"
                      >
                        <span
                          class="w-3.5 h-3.5 rounded border flex items-center justify-center flex-shrink-0"
                          :class="instructionCategoryFilter.includes(cat) ? 'bg-indigo-500 border-indigo-500' : 'border-gray-300'"
                        >
                          <Icon v-if="instructionCategoryFilter.includes(cat)" name="heroicons:check" class="w-2.5 h-2.5 text-white" />
                        </span>
                        <span class="text-gray-700">{{ helpers.formatCategory(cat) }}</span>
                      </button>
                      <button
                        v-if="instructionCategoryFilter.length > 0"
                        @click.stop="instructionCategoryFilter = []"
                        class="w-full px-2.5 py-1.5 text-[11px] text-indigo-600 hover:bg-indigo-50 border-t border-gray-100 text-left font-medium"
                      >
                        Clear all
                      </button>
                    </div>
                  </div>
                </div>
              </div>

              <!-- List -->
              <div v-if="filteredInstructions.length === 0" class="text-xs text-gray-400 italic py-6 text-center">No matching instructions</div>
              <div v-else class="border border-gray-200 rounded-lg overflow-hidden">
                <button
                  v-for="inst in filteredInstructions"
                  :key="inst.id"
                  @click="selectedInstruction = inst"
                  class="w-full px-3 py-2.5 text-left text-xs flex items-start gap-2.5 hover:bg-gray-50 border-b border-gray-100 last:border-b-0 transition-colors"
                >
                  <!-- Source icon -->
                  <div class="flex-shrink-0 mt-0.5">
                    <Icon
                      :name="helpers.getSourceIcon(inst)"
                      class="w-4 h-4"
                      :class="{
                        'text-amber-500': helpers.getSourceType(inst) === 'ai',
                        'text-blue-500': helpers.getSourceType(inst) === 'user',
                        'text-gray-500': helpers.getSourceType(inst) === 'git'
                      }"
                    />
                  </div>
                  <div class="flex-1 min-w-0">
                    <!-- Title or text preview -->
                    <div class="flex items-center gap-1.5">
                      <span class="truncate text-gray-800 font-medium text-xs">{{ inst.title || inst.text?.slice(0, 60) || 'Untitled' }}</span>
                    </div>
                    <!-- Text preview if title exists -->
                    <p v-if="inst.title && inst.text" class="text-[11px] text-gray-500 truncate mt-0.5 leading-snug">{{ inst.text.slice(0, 80) }}</p>
                    <!-- Badges row -->
                    <div class="flex items-center gap-1.5 mt-1 flex-wrap">
                      <span
                        :class="helpers.getCategoryClass(inst.category)"
                        class="text-[9px] px-1 py-0.5 rounded font-medium"
                      >{{ helpers.formatCategory(inst.category) }}</span>
                      <span
                        :class="helpers.getStatusClass(inst)"
                        class="text-[9px] px-1 py-0.5 rounded font-medium"
                      >{{ helpers.formatStatus(inst.status) }}</span>
                      <span
                        :class="helpers.getLoadModeClass(inst.load_mode)"
                        class="text-[9px] px-1 py-0.5 rounded font-medium"
                      >{{ helpers.getLoadModeLabel(inst.load_mode) }}</span>
                      <span
                        v-if="!inst.data_sources?.length"
                        class="px-1 py-0.5 text-[9px] rounded bg-purple-50 text-purple-600 font-medium"
                      >Global</span>
                    </div>
                  </div>
                </button>
              </div>
            </template>
          </template>
        </template>
      </div>

      <!-- Tables tab — uses TablesSelector component -->
      <div v-else-if="activeTab === 'tables'" class="h-full">
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
import InstructionGlobalCreateComponent from '~/components/InstructionGlobalCreateComponent.vue'
import { useInstructionHelpers } from '~/composables/useInstructionHelpers'

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
const activeTab = ref<'instructions' | 'tables' | 'queries' | 'evals'>('instructions')

// Data caches (keyed by agent id)
const instructionsCache = ref<Record<string, any[]>>({})
const queriesCache = ref<Record<string, any[]>>({})
const evalsCache = ref<Record<string, any[]>>({})

// Instruction detail state
const selectedInstruction = ref<any | null>(null)
const instructionLoading = ref(false)

// Instruction helpers & filters
const helpers = useInstructionHelpers()
const instructionSearch = ref('')
const instructionCategoryFilter = ref<string[]>([])
const instructionStatusFilter = ref<string>('published')
const categoryDropdownOpen = ref(false)
const categoryDropdownRef = ref<HTMLElement | null>(null)

const instructionCategories = computed(() => {
  const cats = new Set(instructions.value.map((i: any) => i.category).filter(Boolean))
  return Array.from(cats).sort()
})

const instructionStatuses = computed(() => {
  const statuses = new Set(instructions.value.map((i: any) => i.status).filter(Boolean))
  return Array.from(statuses).sort()
})

function toggleCategoryFilter(cat: string) {
  const idx = instructionCategoryFilter.value.indexOf(cat)
  if (idx >= 0) {
    instructionCategoryFilter.value = instructionCategoryFilter.value.filter(c => c !== cat)
  } else {
    instructionCategoryFilter.value = [...instructionCategoryFilter.value, cat]
  }
}

function onCategoryDropdownOutsideClick(e: MouseEvent) {
  if (categoryDropdownRef.value && !categoryDropdownRef.value.contains(e.target as Node)) {
    categoryDropdownOpen.value = false
  }
}

onMounted(() => {
  document.addEventListener('click', onCategoryDropdownOutsideClick)
})
onUnmounted(() => {
  document.removeEventListener('click', onCategoryDropdownOutsideClick)
})

const filteredInstructions = computed(() => {
  let list = instructions.value
  const q = instructionSearch.value.toLowerCase().trim()
  if (q) {
    list = list.filter((i: any) =>
      (i.title || '').toLowerCase().includes(q) ||
      (i.text || '').toLowerCase().includes(q)
    )
  }
  if (instructionCategoryFilter.value.length > 0) {
    list = list.filter((i: any) => instructionCategoryFilter.value.includes(i.category))
  }
  if (instructionStatusFilter.value) {
    list = list.filter((i: any) => i.status === instructionStatusFilter.value)
  }
  return list
})

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
  { key: 'instructions' as const, label: 'Instructions', count: instructions.value.length },
  { key: 'tables' as const, label: 'Tables', count: 0 },
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

function onInstructionSaved() {
  selectedInstruction.value = null
  // Invalidate cache so list refreshes
  if (selectedAgentId.value) {
    delete instructionsCache.value[selectedAgentId.value]
    fetchTabData(selectedAgentId.value, 'instructions')
  }
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
        query: { data_source_ids: agentId, include_global: true, limit: 50, include_own: true, include_drafts: true }
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
  activeTab.value = 'instructions'
  selectedInstruction.value = null
  instructionsError.value = null
  queriesError.value = null
  evalsError.value = null
  instructionSearch.value = ''
  instructionCategoryFilter.value = []
  instructionStatusFilter.value = 'published'
})

// Expose methods for external callers
function openInstruction(instruction: any) {
  activeTab.value = 'instructions'
  instructionLoading.value = false
  selectedInstruction.value = instruction
}

function setInstructionLoading(value: boolean) {
  activeTab.value = 'instructions'
  if (value) {
    selectedInstruction.value = null
  }
  instructionLoading.value = value
}

defineExpose({ openInstruction, setInstructionLoading })
</script>
