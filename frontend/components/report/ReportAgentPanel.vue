<template>
  <div class="h-full flex flex-col overflow-hidden">
    <!-- Agent selector dropdown -->
    <div class="px-4 pt-4 pb-2 flex-shrink-0 bg-gradient-to-b from-indigo-50/40 to-transparent">
      <div v-if="agents.length === 0" class="text-xs text-gray-400 italic text-center py-4">
        {{ $t('reportAgent.noAgents') }}
      </div>
      <div v-else-if="agents.length === 1" class="flex items-center gap-2">
        <button v-if="showClose" @click="$emit('close')" class="hover:bg-gray-100 p-1 rounded">
          <Icon name="heroicons:x-mark" class="w-4 h-4 text-gray-500" />
        </button>
        <DataSourceIcon :type="agents[0].type || agents[0].connections?.[0]?.type" class="h-5 flex-shrink-0" />
        <span class="text-sm font-semibold text-gray-900 truncate">{{ agents[0].name }}</span>
      </div>
      <div v-else class="flex items-center gap-2">
        <button v-if="showClose" @click="$emit('close')" class="hover:bg-gray-100 p-1 rounded flex-shrink-0">
          <Icon name="heroicons:x-mark" class="w-4 h-4 text-gray-500" />
        </button>
        <div class="relative flex-1" ref="dropdownRef">
        <button
          @click="dropdownOpen = !dropdownOpen"
          class="w-full flex items-center gap-2 px-3 py-2 border border-gray-200 rounded-lg text-sm hover:bg-gray-50 transition-colors bg-white/80"
        >
          <DataSourceIcon v-if="selectedAgent" :type="selectedAgent.type || selectedAgent.connections?.[0]?.type" class="h-5 flex-shrink-0" />
          <span class="truncate flex-1 text-start font-medium text-gray-900">
            {{ selectedAgent?.name || $t('reportAgent.selectAgent') }}
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
              <span class="truncate flex-1 text-start font-medium">{{ agent.name }}</span>
              <Icon v-if="selectedAgentId === agent.id" name="heroicons:check" class="w-3.5 h-3.5 text-indigo-600 flex-shrink-0" />
            </button>
          </div>
        </Transition>
        </div>
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
          <span v-if="tab.count > 0" class="ms-1 text-[10px] text-gray-400">({{ tab.count }})</span>
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

          <!-- Instruction form view (edit or create) -->
          <div v-else-if="selectedInstruction || creatingInstruction" class="flex flex-col h-full -m-4">
            <button
              @click="closeInstructionForm"
              class="flex items-center gap-1 px-4 pt-3 pb-2 text-xs text-gray-500 hover:text-gray-700 flex-shrink-0"
            >
              <Icon name="heroicons:chevron-left" class="w-3 h-3 rtl-flip" />
              {{ $t('reportAgent.allInstructions') }}
            </button>
            <!-- Unpublished-build warning banner (edit mode only) -->
            <div
              v-if="selectedInstruction && selectedInstruction.current_build_id && ['draft', 'pending_approval'].includes(selectedInstruction.current_build_status)"
              class="mx-4 mb-2 px-2.5 py-1.5 rounded-md border border-amber-200 bg-amber-50 flex items-start gap-2"
            >
              <Icon name="heroicons:exclamation-triangle" class="w-3.5 h-3.5 text-amber-500 mt-0.5 shrink-0" />
              <div class="flex-1 min-w-0">
                <div class="text-[11px] text-amber-800">
                  {{ selectedInstruction.current_build_status === 'draft'
                    ? $t('reportAgent.unpublishedDraft')
                    : $t('reportAgent.pendingReview') }}
                </div>
                <button
                  v-if="canViewBuilds"
                  @click="openBuildExplorer(selectedInstruction.current_build_id)"
                  class="mt-0.5 text-[11px] text-amber-700 hover:text-amber-900 underline"
                >
                  {{ $t('reportAgent.viewChanges') }}
                </button>
              </div>
            </div>
            <InstructionGlobalCreateComponent
              :key="selectedInstruction?.id || 'new'"
              :instruction="selectedInstruction || undefined"
              :default-status="canCreateInstructions ? 'published' : 'draft'"
              @instruction-saved="onInstructionSaved"
              @cancel="closeInstructionForm"
            />
          </div>

          <!-- Instructions list -->
          <template v-else>
            <div v-if="instructionsError" class="text-xs text-gray-500">{{ instructionsError }}</div>
            <template v-else>
              <!-- Filters -->
              <div class="flex flex-col gap-2 mb-3">
                <div class="relative">
                  <Icon name="heroicons:magnifying-glass" class="absolute start-2 top-1/2 -translate-y-1/2 w-3 h-3 text-gray-400" />
                  <input
                    v-model="instructionSearch"
                    type="text"
                    :placeholder="$t('reportAgent.searchPlaceholder')"
                    class="w-full ps-7 pe-2 py-1.5 text-[11px] border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-300 focus:border-indigo-300"
                  />
                </div>
                <div class="flex items-center gap-2">
                  <!-- Status multi-select -->
                  <div class="relative" ref="statusDropdownRef">
                    <button
                      @click.stop="statusDropdownOpen = !statusDropdownOpen"
                      class="flex items-center gap-1 text-[11px] border border-gray-200 rounded-md py-1 px-2 text-gray-600 hover:bg-gray-50 bg-white"
                    >
                      <span v-if="instructionStatusFilter.length === 0">{{ $t('reportAgent.allStatuses') }}</span>
                      <span v-else class="truncate max-w-[100px]">{{ instructionStatusFilter.map(s => helpers.formatStatus(s)).join(', ') }}</span>
                      <Icon name="heroicons:chevron-down" class="w-3 h-3 text-gray-400 transition-transform" :class="{ 'rotate-180': statusDropdownOpen }" />
                    </button>
                    <div v-if="statusDropdownOpen" class="absolute z-20 mt-1 start-0 bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden min-w-[120px]">
                      <button
                        v-for="s in instructionStatuses"
                        :key="s"
                        @click.stop="toggleStatusFilter(s)"
                        class="w-full flex items-center gap-2 px-2.5 py-1.5 text-[11px] hover:bg-gray-50 transition-colors text-start"
                      >
                        <span
                          class="w-3.5 h-3.5 rounded border flex items-center justify-center flex-shrink-0"
                          :class="instructionStatusFilter.includes(s) ? 'bg-indigo-500 border-indigo-500' : 'border-gray-300'"
                        >
                          <Icon v-if="instructionStatusFilter.includes(s)" name="heroicons:check" class="w-2.5 h-2.5 text-white" />
                        </span>
                        <span class="text-gray-700">{{ helpers.formatStatus(s) }}</span>
                      </button>
                      <button
                        v-if="instructionStatusFilter.length > 0"
                        @click.stop="instructionStatusFilter = []"
                        class="w-full px-2.5 py-1.5 text-[11px] text-indigo-600 hover:bg-indigo-50 border-t border-gray-100 text-start font-medium"
                      >
                        {{ $t('reportAgent.clearAll') }}
                      </button>
                    </div>
                  </div>
                  <!-- Category multi-select -->
                  <div class="relative" ref="categoryDropdownRef">
                    <button
                      @click.stop="categoryDropdownOpen = !categoryDropdownOpen"
                      class="flex items-center gap-1 text-[11px] border border-gray-200 rounded-md py-1 px-2 text-gray-600 hover:bg-gray-50 bg-white"
                    >
                      <span v-if="instructionCategoryFilter.length === 0">{{ $t('reportAgent.allCategories') }}</span>
                      <span v-else class="truncate max-w-[100px]">{{ instructionCategoryFilter.map(c => helpers.formatCategory(c)).join(', ') }}</span>
                      <Icon name="heroicons:chevron-down" class="w-3 h-3 text-gray-400 transition-transform" :class="{ 'rotate-180': categoryDropdownOpen }" />
                    </button>
                    <div v-if="categoryDropdownOpen" class="absolute z-20 mt-1 start-0 bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden min-w-[140px]">
                      <button
                        v-for="cat in instructionCategories"
                        :key="cat"
                        @click.stop="toggleCategoryFilter(cat)"
                        class="w-full flex items-center gap-2 px-2.5 py-1.5 text-[11px] hover:bg-gray-50 transition-colors text-start"
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
                        class="w-full px-2.5 py-1.5 text-[11px] text-indigo-600 hover:bg-indigo-50 border-t border-gray-100 text-start font-medium"
                      >
                        {{ $t('reportAgent.clearAll') }}
                      </button>
                    </div>
                  </div>
                  <!-- Create / Suggest button -->
                  <UButton
                    size="xs"
                    color="blue"
                    icon="i-heroicons-plus"
                    class="ms-auto"
                    @click="creatingInstruction = true"
                  >
                    {{ canCreateInstructions ? $t('reportAgent.create') : $t('reportAgent.suggest') }}
                  </UButton>
                </div>
              </div>

              <!-- List -->
              <div v-if="instructions.length === 0" class="text-xs text-gray-400 italic py-6 text-center">{{ $t('reportAgent.noInstructions') }}</div>
              <div v-else-if="filteredInstructions.length === 0" class="text-xs text-gray-400 italic py-6 text-center">{{ $t('reportAgent.noMatching') }}</div>
              <div v-else class="border border-gray-200 rounded-lg overflow-hidden">
                <button
                  v-for="inst in filteredInstructions"
                  :key="inst.id"
                  @click="selectedInstruction = inst"
                  class="w-full px-3 py-2.5 text-start text-xs flex items-start gap-2.5 hover:bg-gray-50 border-b border-gray-100 last:border-b-0 transition-colors"
                >
                  <div class="flex-1 min-w-0">
                    <!-- Title or text preview -->
                    <div class="flex items-center gap-1.5">
                      <span class="truncate text-gray-800 font-medium text-xs">{{ inst.title || inst.text?.slice(0, 60) || $t('reportAgent.untitled') }}</span>
                    </div>
                    <!-- Text preview if title exists -->
                    <p v-if="inst.title && inst.text" class="text-[11px] text-gray-500 truncate mt-0.5 leading-snug">{{ inst.text.slice(0, 80) }}</p>
                    <!-- Badges row -->
                    <div class="flex items-center gap-1.5 mt-1 flex-wrap">
                      <!-- Data source indicator -->
                      <template v-if="inst.data_sources?.length">
                        <span v-for="ds in inst.data_sources" :key="ds.id" class="inline-flex items-center gap-0.5 px-1 py-0.5 rounded bg-gray-50 text-[9px] text-gray-600 font-medium border border-gray-100">
                          <DataSourceIcon :type="getInstructionDsType(ds)" class="h-2.5 flex-shrink-0" />
                          <span class="truncate max-w-[80px]">{{ ds.name }}</span>
                        </span>
                      </template>
                      <span v-else class="inline-flex items-center gap-0.5 px-1 py-0.5 rounded bg-purple-50 text-[9px] text-purple-600 font-medium border border-purple-100">
                        <Icon name="heroicons:circle-stack" class="w-2.5 h-2.5 text-purple-400 flex-shrink-0" />
                        {{ $t('reportAgent.any') }}
                      </span>
                      <span
                        :class="helpers.getCategoryClass(inst.category)"
                        class="text-[9px] px-1 py-0.5 rounded font-medium"
                      >{{ helpers.formatCategory(inst.category) }}</span>
                      <span
                        :class="helpers.getStatusClass(inst)"
                        class="text-[9px] px-1 py-0.5 rounded font-medium"
                      >{{ helpers.getStatusLabel(inst) }}</span>
                      <span
                        :class="helpers.getLoadModeClass(inst.load_mode)"
                        class="text-[9px] px-1 py-0.5 rounded font-medium"
                      >{{ helpers.getLoadModeLabel(inst.load_mode) }}</span>
                      <!-- User & date -->
                      <span v-if="inst.user" class="inline-flex items-center gap-0.5 text-[9px] text-gray-400">
                        <Icon name="heroicons:user" class="w-2.5 h-2.5 flex-shrink-0" />
                        <span class="truncate max-w-[60px]">{{ inst.user.name || inst.user.email }}</span>
                        <span>· {{ formatDate(inst.created_at) }}</span>
                      </span>
                    </div>
                    <!-- References -->
                    <div v-if="inst.references?.length" class="flex items-center gap-1 mt-1 flex-wrap">
                      <Icon name="heroicons:link" class="w-2.5 h-2.5 text-gray-300 flex-shrink-0" />
                      <span
                        v-for="ref in inst.references"
                        :key="ref.id"
                        class="inline-flex items-center gap-0.5 px-1 py-0.5 rounded bg-slate-50 border border-slate-100 text-[9px] text-slate-600 font-medium"
                      >
                        <Icon :name="ref.object_type === 'datasource_table' ? 'heroicons:table-cells' : ref.object_type === 'instruction' ? 'heroicons:document-text' : 'heroicons:cube'" class="w-2.5 h-2.5 flex-shrink-0" :class="ref.object_type === 'instruction' ? 'text-indigo-400' : 'text-slate-400'" />
                        <span class="truncate max-w-[100px]">{{ ref.display_text || ref.object?.name || ref.object?.title || ref.object_id }}</span>
                      </span>
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
          :schema="canUpdateDataSource ? 'full' : 'user'"
          :can-update="canUpdateDataSource"
          :show-refresh="false"
          :show-save="canUpdateDataSource"
          :save-label="t('reportAgent.save')"
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
          <div v-else-if="queries.length === 0" class="text-xs text-gray-400 italic py-6 text-center">{{ $t('reportAgent.noQueries') }}</div>
          <div v-else class="border border-gray-200 rounded-lg overflow-hidden">
            <a
              v-for="entity in queries"
              :key="entity.id"
              :href="`/queries/${entity.id}`"
              class="w-full px-3 py-2 text-start text-xs flex items-start gap-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0 block"
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
          <div v-else-if="evals.length === 0" class="text-xs text-gray-400 italic py-6 text-center">{{ $t('reportAgent.noEvals') }}</div>
          <div v-else class="border border-gray-200 rounded-lg overflow-hidden">
            <a
              v-for="tc in evals"
              :key="tc.id"
              :href="`/evals`"
              class="w-full px-3 py-2 text-start text-xs flex items-start gap-2 hover:bg-gray-50 border-b border-gray-100 last:border-b-0 block"
            >
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-1.5">
                  <span class="px-1 py-0.5 text-[9px] rounded border flex-shrink-0 text-amber-700 border-amber-200 bg-amber-50">
                    {{ tc.suite_name || $t('reportAgent.evalBadge') }}
                  </span>
                  <span class="truncate text-gray-800 font-medium">{{ tc.name || promptPreview(tc.prompt_json) }}</span>
                </div>
                <div v-if="tc.expectations_json?.length" class="text-[11px] text-gray-400 truncate mt-0.5">
                  {{ tc.expectations_json.length === 1 ? $t('reportAgent.expectationOne', { n: 1 }) : $t('reportAgent.expectationMany', { n: tc.expectations_json.length }) }}
                </div>
              </div>
            </a>
          </div>
        </template>
      </div>
    </div>

    <BuildExplorerModal
      v-if="canViewBuilds"
      v-model="showBuildExplorer"
      :build-id="buildExplorerBuildId"
    />
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import TablesSelector from '~/components/datasources/TablesSelector.vue'
import InstructionGlobalCreateComponent from '~/components/InstructionGlobalCreateComponent.vue'
import BuildExplorerModal from '~/components/instructions/BuildExplorerModal.vue'
import { useInstructionHelpers } from '~/composables/useInstructionHelpers'

const { t } = useI18n()

const props = defineProps<{
  agents: Array<{ id: string; name: string; type?: string; connections?: any[] }>
  showClose?: boolean
}>()

const emit = defineEmits(['close'])

// Permissions
const canUpdateDataSource = computed(() => useCan('update_data_source'))
const canViewBuilds = computed(() => useCan('view_builds'))
const canManageTests = computed(() => useCan('manage_tests'))

// Build explorer modal (for "View changes" affordance on unpublished-build banner)
const showBuildExplorer = ref(false)
const buildExplorerBuildId = ref<string>('')
const openBuildExplorer = (bid: string) => {
  buildExplorerBuildId.value = bid
  showBuildExplorer.value = true
}

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
const creatingInstruction = ref(false)
const instructionLoading = ref(false)

const canCreateInstructions = computed(() => {
  if (useCan('manage_instructions')) return true
  if (!selectedAgentId.value) return false
  return useCan('manage_instructions', { type: 'data_source', id: selectedAgentId.value })
})

function closeInstructionForm() {
  selectedInstruction.value = null
  creatingInstruction.value = false
}

// Instruction helpers & filters
const helpers = useInstructionHelpers()
const instructionSearch = ref('')
const instructionCategoryFilter = ref<string[]>([])
const instructionStatusFilter = ref<string[]>(['published'])
const statusDropdownOpen = ref(false)
const statusDropdownRef = ref<HTMLElement | null>(null)
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

function toggleStatusFilter(status: string) {
  const idx = instructionStatusFilter.value.indexOf(status)
  if (idx >= 0) {
    instructionStatusFilter.value = instructionStatusFilter.value.filter(s => s !== status)
  } else {
    instructionStatusFilter.value = [...instructionStatusFilter.value, status]
  }
}

function onCategoryDropdownOutsideClick(e: MouseEvent) {
  if (categoryDropdownRef.value && !categoryDropdownRef.value.contains(e.target as Node)) {
    categoryDropdownOpen.value = false
  }
  if (statusDropdownRef.value && !statusDropdownRef.value.contains(e.target as Node)) {
    statusDropdownOpen.value = false
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
  if (instructionStatusFilter.value.length > 0) {
    list = list.filter((i: any) => instructionStatusFilter.value.includes(i.status))
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
const tabs = computed(() => {
  const out: Array<{ key: 'instructions' | 'tables' | 'queries' | 'evals'; label: string; count: number }> = [
    { key: 'instructions', label: t('reportAgent.tabInstructions'), count: instructions.value.length },
    { key: 'tables', label: t('reportAgent.tabTables'), count: 0 },
    { key: 'queries', label: t('reportAgent.tabQueries'), count: queries.value.length },
  ]
  if (canManageTests.value) {
    out.push({ key: 'evals', label: t('reportAgent.tabEvals'), count: evals.value.length })
  }
  return out
})

const instructions = computed(() => selectedAgentId.value ? (instructionsCache.value[selectedAgentId.value] || []) : [])
const queries = computed(() => selectedAgentId.value ? (queriesCache.value[selectedAgentId.value] || []) : [])
const evals = computed(() => selectedAgentId.value ? (evalsCache.value[selectedAgentId.value] || []) : [])

// Resolve DS type for instruction's embedded data_source (may lack connections)
function getInstructionDsType(ds: any): string | undefined {
  // Try the DS object itself
  if (ds.type) return ds.type
  if (ds.connections?.[0]?.type) return ds.connections[0].type
  // Look up from agents prop (which has full connection info)
  const match = props.agents.find(a => a.id === ds.id)
  return match?.type || match?.connections?.[0]?.type || undefined
}

function selectAgent(agentId: string) {
  selectedAgentId.value = agentId
  dropdownOpen.value = false
}

function onInstructionSaved() {
  selectedInstruction.value = null
  creatingInstruction.value = false
  // Invalidate cache so list refreshes
  if (selectedAgentId.value) {
    delete instructionsCache.value[selectedAgentId.value]
    fetchTabData(selectedAgentId.value, 'instructions')
  }
}

function formatDate(dateStr: string): string {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function promptPreview(promptJson: any): string {
  if (!promptJson) return t('reportAgent.untitled')
  if (typeof promptJson === 'string') return promptJson.slice(0, 60)
  if (Array.isArray(promptJson) && promptJson.length > 0) {
    const first = promptJson[0]
    const content = first?.content || first?.text || ''
    return typeof content === 'string' ? content.slice(0, 60) : t('reportAgent.untitled')
  }
  return t('reportAgent.untitled')
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
      // Fetch instructions scoped to the selected agent AND global (any data source) instructions
      const { data, error } = await useMyFetch('/api/instructions', {
        method: 'GET',
        query: { data_source_ids: agentId, include_global: true, limit: 200, include_own: true, include_drafts: true }
      })
      if (error?.value) { instructionsError.value = t('reportAgent.loadFailInstructions'); return }
      const payload: any = (data as any)?.value
      const agentInstructions = payload?.items || payload || []
      instructionsCache.value[agentId] = agentInstructions
    } catch { instructionsError.value = t('reportAgent.loadFailInstructions') }
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
      if (error?.value) { queriesError.value = t('reportAgent.loadFailQueries'); return }
      const payload: any = (data as any)?.value
      const entities = Array.isArray(payload) ? payload : []
      queriesCache.value[agentId] = entities.filter((e: any) => e.status === 'published' && e.global_status === 'approved')
    } catch { queriesError.value = t('reportAgent.loadFailQueries') }
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
      if (error?.value) { evalsError.value = t('reportAgent.loadFailEvals'); return }
      const payload: any = (data as any)?.value
      const cases = Array.isArray(payload) ? payload : []
      evalsCache.value[agentId] = cases.filter((c: any) => {
        const dsIds = c.data_source_ids_json || []
        return dsIds.includes(agentId)
      })
    } catch { evalsError.value = t('reportAgent.loadFailEvals') }
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
  creatingInstruction.value = false
  instructionsError.value = null
  queriesError.value = null
  evalsError.value = null
  instructionSearch.value = ''
  instructionCategoryFilter.value = []
  instructionStatusFilter.value = ['published']
})

// Expose methods for external callers
function openInstruction(instruction: any) {
  activeTab.value = 'instructions'
  instructionLoading.value = false
  creatingInstruction.value = false
  selectedInstruction.value = instruction
}

function setInstructionLoading(value: boolean) {
  activeTab.value = 'instructions'
  if (value) {
    selectedInstruction.value = null
    creatingInstruction.value = false
  }
  instructionLoading.value = value
}

defineExpose({ openInstruction, setInstructionLoading })
</script>
