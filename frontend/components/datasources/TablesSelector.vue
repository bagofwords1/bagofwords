<template>
  <div class="w-full">
    <div v-if="showHeader" class="mb-2 flex items-center justify-between">
      <div>
        <h1 class="text-lg font-semibold">{{ headerTitle }}</h1>
        <p class="text-gray-500 text-sm">{{ headerSubtitle }}</p>
      </div>
      <div>
        <button
          v-if="showRefresh"
          @click="onRefresh"
          :disabled="loading || refreshing"
          :class="refreshIconOnly ? 'p-1.5 rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50' : 'flex items-center gap-2 border border-gray-300 rounded-lg px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50'"
        >
          <Spinner v-if="loading || refreshing" class="w-4 h-4" />
          <span v-if="!refreshIconOnly">Reload tables</span>
        </button>
      </div>
    </div>
    <div v-else class="mb-2 flex items-center justify-end">
      <button
        v-if="showRefresh"
        @click="onRefresh"
        :disabled="loading || refreshing"
        :class="refreshIconOnly ? 'p-1.5 rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50' : 'flex items-center gap-2 border border-gray-300 rounded-lg px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50'"
      >
        <Spinner v-if="loading || refreshing" class="w-4 h-4" />
        <span v-if="!refreshIconOnly">Reload tables</span>
      </button>
    </div>

    <!-- Search and filters row -->
    <div>
      <div class="relative flex items-center gap-1.5">
        <input 
          v-model="searchInput" 
          @input="onSearchInput"
          type="text" 
          placeholder="Search tables..." 
          class="border border-gray-300 rounded px-2 py-1.5 w-full h-7 text-xs focus:outline-none focus:border-blue-500" 
        />
        
        <!-- Filter button (contains both status and schema filters) -->
        <button
          ref="filterButtonRef"
          type="button"
          @click="toggleFilterMenu"
          class="h-7 w-7 inline-flex items-center justify-center rounded border"
          :class="hasActiveFilters ? 'border-blue-500 bg-blue-50 text-blue-700' : 'border-gray-300 text-gray-700 hover:bg-gray-50'"
          aria-label="Filter tables"
        >
          <UIcon name="heroicons-funnel" class="w-4 h-4" />
        </button>
        
        <!-- Sort -->
        <button
          ref="sortButtonRef"
          type="button"
          @click="toggleSortMenu"
          class="h-7 w-7 inline-flex items-center justify-center rounded border border-gray-300 text-gray-700 hover:bg-gray-50"
          aria-label="Sort tables"
        >
          <UIcon name="heroicons-arrows-up-down" class="w-4 h-4" />
        </button>
        
        <!-- Filter menu (multi-level with status and schema) -->
        <div
          v-if="filterMenuOpen"
          ref="filterMenuRef"
          class="absolute right-8 top-full mt-1 z-20 bg-white border border-gray-200 rounded shadow-lg w-48"
        >
          <!-- Status filter section -->
          <div class="py-1 border-b border-gray-100">
            <div class="px-2 py-1 text-[10px] font-medium text-gray-400 uppercase tracking-wider">Status</div>
            <button
              type="button"
              class="w-full text-left px-2 py-1 text-xs hover:bg-gray-50 flex items-center justify-between"
              @click="setSelectedFilter('selected')"
            >
              <span>Selected</span>
              <UIcon v-if="filters.selectedState === 'selected'" name="heroicons-check" class="w-3 h-3 text-blue-600" />
            </button>
            <button
              type="button"
              class="w-full text-left px-2 py-1 text-xs hover:bg-gray-50 flex items-center justify-between"
              @click="setSelectedFilter('unselected')"
            >
              <span>Unselected</span>
              <UIcon v-if="filters.selectedState === 'unselected'" name="heroicons-check" class="w-3 h-3 text-blue-600" />
            </button>
          </div>
          
          <!-- Schema filter section -->
          <div class="py-1">
            <div class="px-2 py-1 text-[10px] font-medium text-gray-400 uppercase tracking-wider flex items-center justify-between">
              <span>Schema</span>
              <button
                v-if="selectedSchemas.length > 0"
                type="button"
                @click.stop="clearSchemaFilter"
                class="text-[9px] text-gray-400 hover:text-gray-600"
              >
                Clear
              </button>
            </div>
            <div v-if="availableSchemas.length === 0" class="px-2 py-1 text-xs text-gray-400">No schemas</div>
            <div v-else class="max-h-32 overflow-y-auto">
              <label
                v-for="schema in availableSchemas"
                :key="schema"
                class="flex items-center px-2 py-1 text-xs hover:bg-gray-50 cursor-pointer"
              >
                <input
                  type="checkbox"
                  :checked="selectedSchemas.includes(schema)"
                  @change="toggleSchemaFilter(schema)"
                  class="mr-1.5 h-3 w-3 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                />
                <span class="truncate">{{ schema }}</span>
              </label>
            </div>
          </div>

          <!-- Connection filter section -->
          <div v-if="availableConnections.length >= 1" class="py-1 border-t border-gray-100">
            <div class="px-2 py-1 text-[10px] font-medium text-gray-400 uppercase tracking-wider flex items-center justify-between">
              <span>Connection</span>
              <button
                v-if="selectedConnections.length > 0"
                type="button"
                @click.stop="clearConnectionFilter"
                class="text-[9px] text-gray-400 hover:text-gray-600"
              >
                Clear
              </button>
            </div>
            <div class="max-h-32 overflow-y-auto">
              <label
                v-for="conn in availableConnections"
                :key="conn.id"
                class="flex items-center px-2 py-1 text-xs hover:bg-gray-50 cursor-pointer"
              >
                <input
                  type="checkbox"
                  :checked="selectedConnections.includes(conn.id)"
                  @change="toggleConnectionFilter(conn.id)"
                  class="mr-1.5 h-3 w-3 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                />
                <span class="truncate">{{ conn.name }}</span>
                <span class="ml-1 text-[9px] text-gray-400">({{ conn.type }})</span>
              </label>
            </div>
          </div>

          <!-- Clear all filters -->
          <div v-if="hasActiveFilters" class="border-t border-gray-100 p-1.5">
            <button
              type="button"
              @click="clearAllFilters"
              class="w-full text-[10px] text-gray-500 hover:text-gray-700 py-0.5"
            >
              Clear all filters
            </button>
          </div>
        </div>
        
        <!-- Sort menu -->
        <div
          v-if="sortMenuOpen"
          ref="sortMenuRef"
          class="absolute right-0 top-full mt-1 z-20 bg-white border border-gray-200 rounded shadow-lg w-32"
        >
          <div class="py-1">
            <button
              type="button"
              class="w-full text-left px-2 py-1 text-xs hover:bg-gray-50 flex items-center justify-between"
              @click="setSort('name')"
            >
              <span>Name</span>
              <UIcon v-if="sort.key === 'name'" name="heroicons-check" class="w-3 h-3 text-blue-600" />
            </button>
            <button
              type="button"
              class="w-full text-left px-2 py-1 text-xs hover:bg-gray-50 flex items-center justify-between"
              @click="setSort('is_active')"
            >
              <span>Selected</span>
              <UIcon v-if="sort.key === 'is_active'" name="heroicons-check" class="w-3 h-3 text-blue-600" />
            </button>
            <button
              v-if="props.showStats"
              type="button"
              class="w-full text-left px-2 py-1 text-xs hover:bg-gray-50 flex items-center justify-between"
              @click="setSort('usage')"
            >
              <span>Usage</span>
              <UIcon v-if="sort.key === 'usage'" name="heroicons-check" class="w-3 h-3 text-blue-600" />
            </button>
          </div>
        </div>
      </div>
      
      <!-- Stats row -->
      <div class="mt-1 text-[10px] text-gray-500 flex items-center justify-between">
        <span v-if="isPaginated && hasActiveFilters">
          {{ totalMatching }} matching · Showing {{ paginationStart }}-{{ paginationEnd }}
        </span>
        <span v-else-if="isPaginated">
          Showing {{ paginationStart }}-{{ paginationEnd }} of {{ totalTables }}
        </span>
        <span v-else></span>
        
        <!-- Right side: bulk actions -->
        <div v-if="canUpdate" class="flex items-center gap-2">
          <button
            @click="selectAllMatching"
            :disabled="loading || refreshing || bulkUpdating"
            class="px-2 py-0.5 text-[10px] rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            <span v-if="bulkUpdating">...</span>
            <span v-else-if="hasActiveFilters">Select all ({{ totalMatching }})</span>
            <span v-else>Select all</span>
          </button>
          <button
            @click="deselectAllMatching"
            :disabled="loading || refreshing || bulkUpdating"
            class="px-2 py-0.5 text-[10px] rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            <span v-if="bulkUpdating">...</span>
            <span v-else-if="hasActiveFilters">Deselect all ({{ totalMatching }})</span>
            <span v-else>Deselect all</span>
          </button>
        </div>
      </div>
      
      <!-- Active count row -->
      <div class="text-[10px] text-gray-500">
        {{ selectedCount }}/{{ totalTables }} active
      </div>
    </div>

    <!-- Loading state -->
    <div v-if="loading" class="text-sm text-gray-500 py-10 flex items-center justify-center">
      <Spinner class="w-4 h-4 mr-2" />
      Loading schema...
    </div>

    <!-- Tables list -->
    <div v-else class="flex-1 flex flex-col h-full">
      <div v-if="tables.length === 0" class="text-sm text-gray-500 py-4">No tables found.</div>
      <div v-else class="flex-1 flex flex-col min-h-full">
        <div class="flex-1 overflow-y-auto min-h-0 mt-2" :style="{ maxHeight }">
          <ul class="divide-y divide-gray-100">
            <li v-for="table in tables" :key="table.name" class="py-2 px-2">
              <div class="flex items-center">
                <UCheckbox 
                  v-if="canUpdate" 
                  color="blue" 
                  :model-value="isTableActive(table.name)"
                  @update:model-value="(val: boolean) => onTableToggle(table.name, val)"
                  class="mr-3" 
                />
                <button type="button" class="flex items-center justify-between text-left flex-1" @click="toggleTableExpand(table)">
                  <div class="flex items-center min-w-0">
                    <UIcon :name="expandedTables[table.name] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 mr-1 text-gray-500" />
                    <DataSourceIcon v-if="availableConnections.length > 1" :type="table.connection_type" class="h-3.5 mr-1.5 flex-shrink-0" />
                    <span class="text-sm text-gray-800 truncate">{{ table.name }}</span>
                    <span v-if="!isTableActive(table.name) && canUpdate" class="ml-2 text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-500">inactive</span>
                    <span v-if="isTableDirty(table.name)" class="ml-1 text-[10px] px-1 py-0.5 rounded bg-yellow-100 text-yellow-700">modified</span>
                  </div>
                  <span v-if="props.showStats && (table.usage_count !== undefined)" class="ml-2 text-[11px] text-gray-500 whitespace-nowrap flex items-center gap-2">
                    <span>usage {{ table.usage_count }}</span>
                    <UTooltip text="Successful executed queries">
                      <span class="inline-flex items-center gap-1">
                        <UIcon name="heroicons-check-circle" class="w-3 h-3 text-green-600" />
                        <span>{{ table.success_count ?? 0 }}</span>
                      </span>
                    </UTooltip>
                    <UTooltip text="Failed executed queries">
                      <span class="inline-flex items-center gap-1">
                        <UIcon name="heroicons-x-circle" class="w-3 h-3 text-red-600" />
                        <span>{{ table.failure_count ?? 0 }}</span>
                      </span>
                    </UTooltip>
                    <UTooltip text="Positive feedback">
                      <span class="inline-flex items-center gap-1">
                        <UIcon name="heroicons-hand-thumb-up" class="w-3 h-3 text-green-600" />
                        <span>{{ table.pos_feedback_count ?? 0 }}</span>
                      </span>
                    </UTooltip>
                    <UTooltip text="Negative feedback">
                      <span class="inline-flex items-center gap-1">
                        <UIcon name="heroicons-hand-thumb-down" class="w-3 h-3 text-red-600" />
                        <span>{{ table.neg_feedback_count ?? 0 }}</span>
                      </span>
                    </UTooltip>
                  </span>
                </button>
              </div>
              <div v-if="expandedTables[table.name] && table.columns" class="mt-2 ml-7">
                <!-- Columns -->
                <div class="border border-gray-100 rounded">
                  <div class="grid grid-cols-2 text-xs font-medium text-gray-500 bg-gray-50 px-2 py-1 rounded-t">
                    <div>Name</div>
                    <div>Type</div>
                  </div>
                  <div class="divide-y divide-gray-100">
                    <div v-for="col in table.columns" :key="col.name" class="grid grid-cols-2 text-xs px-2 py-1">
                      <div class="text-gray-700">{{ col.name }}</div>
                      <div class="text-gray-500">{{ col.dtype || col.type }}</div>
                    </div>
                  </div>
                </div>

                <!-- Relationships -->
                <div v-if="table.fks?.length" class="mt-2 border border-gray-100 rounded">
                  <div class="text-xs font-medium text-gray-500 bg-gray-50 px-2 py-1 rounded-t">Relationships</div>
                  <div class="divide-y divide-gray-100">
                    <div v-for="(fk, idx) in table.fks" :key="idx" class="text-xs px-2 py-1 text-gray-600">
                      <span class="text-gray-700">{{ fk.column?.name }}</span>
                      <span class="text-gray-400 mx-1">→</span>
                      <span class="text-blue-600">{{ fk.references_name }}</span>
                      <span class="text-gray-400">.</span>
                      <span class="text-gray-700">{{ fk.references_column?.name }}</span>
                    </div>
                  </div>
                </div>

                <!-- Power BI Metadata -->
                <div v-if="table.metadata_json?.powerbi" class="mt-2 text-xs text-gray-500 space-y-0.5">
                  <div v-if="table.metadata_json.powerbi.datasetName">
                    <span class="text-gray-400">Dataset:</span> {{ table.metadata_json.powerbi.datasetName }}
                  </div>
                  <div v-if="table.metadata_json.powerbi.workspaceName">
                    <span class="text-gray-400">Workspace:</span> {{ table.metadata_json.powerbi.workspaceName }}
                  </div>
                  <div v-if="table.metadata_json.powerbi.reports?.length">
                    <span class="text-gray-400">Reports:</span> {{ table.metadata_json.powerbi.reports.map((r: any) => r.name).join(', ') }}
                  </div>
                </div>
              </div>
            </li>
          </ul>
        </div>
        
        <!-- Pagination controls -->
        <div v-if="isPaginated && totalPages > 1" class="mt-3 flex items-center justify-center gap-2">
          <button
            @click="goToPage(1)"
            :disabled="page === 1 || loading"
            class="px-2 py-1 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <UIcon name="heroicons-chevron-double-left" class="w-3 h-3" />
          </button>
          <button
            @click="goToPage(page - 1)"
            :disabled="page === 1 || loading"
            class="px-2 py-1 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <UIcon name="heroicons-chevron-left" class="w-3 h-3" />
          </button>
          <span class="text-xs text-gray-600 px-2">
            Page {{ page }} of {{ totalPages }}
          </span>
          <button
            @click="goToPage(page + 1)"
            :disabled="page >= totalPages || loading"
            class="px-2 py-1 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <UIcon name="heroicons-chevron-right" class="w-3 h-3" />
          </button>
          <button
            @click="goToPage(totalPages)"
            :disabled="page >= totalPages || loading"
            class="px-2 py-1 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <UIcon name="heroicons-chevron-double-right" class="w-3 h-3" />
          </button>
        </div>
      </div>
    </div>

    <!-- Save button -->
    <div v-if="showSave && canUpdate" class="mt-3 flex items-center justify-end">
      <button 
        @click="onSave" 
        :disabled="saving" 
        class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50"
      >
        <span v-if="saving">Saving...</span>
        <span v-else>{{ saveLabel }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '@/components/Spinner.vue'
import DataSourceIcon from '@/components/DataSourceIcon.vue'

type Column = { name: string; dtype?: string; type?: string }
type ForeignKey = {
  column?: { name: string; dtype?: string };
  references_name: string;
  references_column?: { name: string; dtype?: string };
}
type Table = {
  name: string;
  is_active: boolean;
  columns?: Column[];
  pks?: any[];
  fks?: ForeignKey[];
  usage_count?: number;
  success_count?: number;
  failure_count?: number;
  pos_feedback_count?: number;
  neg_feedback_count?: number;
  metadata_json?: {
    schema?: string;
    powerbi?: {
      datasetId?: string;
      datasetName?: string;
      workspaceId?: string;
      workspaceName?: string;
      tableName?: string;
      reports?: { id: string; name: string; webUrl?: string }[];
    };
  };
  connection_id?: string;
  connection_name?: string;
  connection_type?: string;
}

type ConnectionInfo = {
  id: string;
  name: string;
  type: string;
}

type PaginatedResponse = {
  tables: Table[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  schemas: string[];
  connections: ConnectionInfo[];
  selected_count: number;
  total_tables: number;
  has_more: boolean;
}

const props = withDefaults(defineProps<{
  dsId: string;
  schema: 'full' | 'user';
  canUpdate?: boolean;
  showRefresh?: boolean;
  refreshIconOnly?: boolean;
  showSave?: boolean;
  saveLabel?: string;
  maxHeight?: string;
  showHeader?: boolean;
  headerTitle?: string;
  headerSubtitle?: string;
  showStats?: boolean;
  pageSize?: number;
  skipRefreshOnSave?: boolean;
}>(), {
  canUpdate: true,
  showRefresh: true,
  refreshIconOnly: false,
  showSave: true,
  saveLabel: 'Save',
  maxHeight: '50vh',
  showHeader: false,
  headerTitle: 'Select tables',
  headerSubtitle: 'Choose which tables to enable',
  showStats: false,
  pageSize: 100,
  skipRefreshOnSave: false,
})

const emit = defineEmits<{ (e: 'saved', tables: Table[]): void; (e: 'error', err: any): void }>()

const toast = useToast()

// Loading states
const loading = ref(false)
const refreshing = ref(false)
const saving = ref(false)
const bulkUpdating = ref(false)

// Data
const tables = ref<Table[]>([])
const expandedTables = ref<Record<string, boolean>>({})

// Pagination state
const isPaginated = ref(false)
const page = ref(1)
const totalPages = ref(1)
const totalMatching = ref(0)
const totalTables = ref(0)
const selectedCount = ref(0)
const availableSchemas = ref<string[]>([])
const availableConnections = ref<ConnectionInfo[]>([])
const selectedConnections = ref<string[]>([])

// Filter state
const searchInput = ref('')
const searchDebounced = ref('')
const selectedSchemas = ref<string[]>([])
const filters = ref<{ selectedState: 'selected' | 'unselected' | null }>({
  selectedState: null,
})
const sort = reactive<{ key: 'name' | 'is_active' | 'usage' | null; direction: 'asc' | 'desc' }>({
  key: 'is_active',
  direction: 'desc'
})

// Dirty tracking - track changes from original state
const originalActiveState = ref<Map<string, boolean>>(new Map())
const currentActiveState = ref<Map<string, boolean>>(new Map())

// Pending bulk actions (deferred until Save)
type BulkAction = {
  action: 'activate' | 'deactivate'
  filter: Record<string, any> | null
  count: number  // For display purposes
}
const pendingBulkActions = ref<BulkAction[]>([])

// Menu state
const filterMenuOpen = ref(false)
const filterMenuRef = ref<HTMLElement | null>(null)
const filterButtonRef = ref<HTMLElement | null>(null)
const sortMenuOpen = ref(false)
const sortMenuRef = ref<HTMLElement | null>(null)
const sortButtonRef = ref<HTMLElement | null>(null)

// Search debounce
let searchTimeout: ReturnType<typeof setTimeout> | null = null
function onSearchInput() {
  if (searchTimeout) clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => {
    searchDebounced.value = searchInput.value
    page.value = 1
    fetchTables()
  }, 300)
}

// Computed
const paginationStart = computed(() => ((page.value - 1) * props.pageSize) + 1)
const paginationEnd = computed(() => Math.min(page.value * props.pageSize, totalMatching.value))

const hasActiveFilters = computed(() => {
  return searchDebounced.value.trim() !== '' || selectedSchemas.value.length > 0 || selectedConnections.value.length > 0 || filters.value.selectedState !== null
})

const hasPendingChanges = computed(() => {
  if (pendingBulkActions.value.length > 0) return true
  for (const [name, currentVal] of currentActiveState.value) {
    const originalVal = originalActiveState.value.get(name)
    if (originalVal !== currentVal) return true
  }
  return false
})

// Helper functions
function isTableActive(tableName: string): boolean {
  return currentActiveState.value.get(tableName) ?? false
}

function isTableDirty(tableName: string): boolean {
  const original = originalActiveState.value.get(tableName)
  const current = currentActiveState.value.get(tableName)
  return original !== current
}

function onTableToggle(tableName: string, newValue: boolean) {
  currentActiveState.value.set(tableName, newValue)
}

function endpointForSchema(): string {
  return props.schema === 'user' ? 'schema' : 'full_schema'
}

// Menu toggles
function toggleFilterMenu() {
  filterMenuOpen.value = !filterMenuOpen.value
  sortMenuOpen.value = false
}

function toggleSortMenu() {
  sortMenuOpen.value = !sortMenuOpen.value
  filterMenuOpen.value = false
}

function setSelectedFilter(state: 'selected' | 'unselected') {
  filters.value.selectedState = filters.value.selectedState === state ? null : state
  page.value = 1
  fetchTables()
}

function setSort(key: 'name' | 'is_active' | 'usage') {
  if (sort.key === key) {
    sort.direction = sort.direction === 'asc' ? 'desc' : 'asc'
  } else {
    sort.key = key
    sort.direction = key === 'name' ? 'asc' : 'desc'
  }
  sortMenuOpen.value = false
  page.value = 1
  fetchTables()
}

function toggleSchemaFilter(schema: string) {
  const idx = selectedSchemas.value.indexOf(schema)
  if (idx >= 0) {
    selectedSchemas.value.splice(idx, 1)
  } else {
    selectedSchemas.value.push(schema)
  }
  page.value = 1
  fetchTables()
}

function clearSchemaFilter() {
  selectedSchemas.value = []
  page.value = 1
  fetchTables()
}

function toggleConnectionFilter(connectionId: string) {
  const idx = selectedConnections.value.indexOf(connectionId)
  if (idx >= 0) {
    selectedConnections.value.splice(idx, 1)
  } else {
    selectedConnections.value.push(connectionId)
  }
  page.value = 1
  fetchTables()
}

function clearConnectionFilter() {
  selectedConnections.value = []
  page.value = 1
  fetchTables()
}

function clearAllFilters() {
  filters.value.selectedState = null
  selectedSchemas.value = []
  selectedConnections.value = []
  filterMenuOpen.value = false
  page.value = 1
  fetchTables()
}

function onGlobalClick(e: MouseEvent) {
  const target = e.target as Node
  if (filterMenuOpen.value) {
    const inside = (filterMenuRef.value?.contains(target)) || (filterButtonRef.value?.contains(target))
    if (!inside) filterMenuOpen.value = false
  }
  if (sortMenuOpen.value) {
    const inside = (sortMenuRef.value?.contains(target)) || (sortButtonRef.value?.contains(target))
    if (!inside) sortMenuOpen.value = false
  }
}

// Data fetching
async function fetchTables() {
  loading.value = true
  try {
    const endpoint = endpointForSchema()
    
    // For full_schema, use paginated endpoint
    if (props.schema === 'full') {
      const params = new URLSearchParams()
      params.set('page', String(page.value))
      params.set('page_size', String(props.pageSize))
      if (searchDebounced.value.trim()) {
        params.set('search', searchDebounced.value.trim())
      }
      if (selectedSchemas.value.length > 0) {
        params.set('schema_filter', selectedSchemas.value.join(','))
      }
      if (selectedConnections.value.length > 0) {
        params.set('connection_filter', selectedConnections.value.join(','))
      }
      if (sort.key) {
        // Map frontend sort keys to backend
        let sortBy = sort.key
        if (sort.key === 'usage') sortBy = 'centrality_score' // or usage_count if available
        params.set('sort_by', sortBy)
        params.set('sort_dir', sort.direction)
      }
      if (filters.value.selectedState) {
        params.set('selected_state', filters.value.selectedState)
      }
      if (props.showStats) {
        params.set('with_stats', 'true')
      }

      const res = await useMyFetch(`/data_sources/${props.dsId}/${endpoint}?${params.toString()}`, { method: 'GET' })
      
      if ((res as any)?.status?.value === 'success') {
        const data = (res as any).data?.value
        
        // Check if paginated response
        if (data && typeof data === 'object' && 'tables' in data) {
          const paginatedData = data as PaginatedResponse
          isPaginated.value = true
          tables.value = paginatedData.tables
          totalMatching.value = paginatedData.total
          totalPages.value = paginatedData.total_pages
          selectedCount.value = paginatedData.selected_count
          totalTables.value = paginatedData.total_tables
          
          // Update available schemas (only on first load or refresh)
          if (paginatedData.schemas && paginatedData.schemas.length > 0) {
            availableSchemas.value = paginatedData.schemas
          }
          // Update available connections
          if (paginatedData.connections && paginatedData.connections.length > 0) {
            availableConnections.value = paginatedData.connections
          }
          
          // Update tracking maps for loaded tables
          for (const table of paginatedData.tables) {
            if (!originalActiveState.value.has(table.name)) {
              originalActiveState.value.set(table.name, table.is_active)
            }
            // Only set current if not already tracked (preserve local changes)
            if (!currentActiveState.value.has(table.name)) {
              currentActiveState.value.set(table.name, table.is_active)
            }
          }
        } else if (Array.isArray(data)) {
          // Legacy list response
          isPaginated.value = false
          tables.value = data as Table[]
          totalMatching.value = tables.value.length
          totalTables.value = tables.value.length
          selectedCount.value = tables.value.filter(t => t.is_active).length
          totalPages.value = 1
          
          // Extract schemas from metadata_json
          const schemas = new Set<string>()
          for (const t of tables.value) {
            const s = t.metadata_json?.schema
            if (s) schemas.add(s)
          }
          availableSchemas.value = Array.from(schemas).sort()
          
          // Initialize tracking
          for (const table of tables.value) {
            originalActiveState.value.set(table.name, table.is_active)
            currentActiveState.value.set(table.name, table.is_active)
          }
        }
      } else {
        tables.value = []
      }
    } else {
      // User schema - non-paginated
      const url = `/data_sources/${props.dsId}/${endpoint}${props.showStats ? '?with_stats=true' : ''}`
      const res = await useMyFetch(url, { method: 'GET' })
      
      if ((res as any)?.status?.value === 'success') {
        isPaginated.value = false
        tables.value = ((res as any).data?.value || []) as Table[]
        totalMatching.value = tables.value.length
        totalTables.value = tables.value.length
        selectedCount.value = tables.value.filter(t => t.is_active).length
        totalPages.value = 1
        
        for (const table of tables.value) {
          originalActiveState.value.set(table.name, table.is_active)
          currentActiveState.value.set(table.name, table.is_active)
        }
      } else {
        tables.value = []
      }
    }
  } catch (e) {
    emit('error', e)
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

function goToPage(newPage: number) {
  if (newPage < 1 || newPage > totalPages.value) return
  page.value = newPage
  fetchTables()
}

function toggleTableExpand(table: Table) {
  expandedTables.value[table.name] = !expandedTables.value[table.name]
}

// Bulk actions - stored as pending operations, executed on Save
function selectAllMatching() {
  // Build filter object matching current filters
  const filterObj: Record<string, any> = {}
  if (selectedSchemas.value.length > 0) {
    filterObj.schema = selectedSchemas.value
  }
  if (selectedConnections.value.length > 0) {
    filterObj.connection = selectedConnections.value
  }
  if (searchDebounced.value.trim()) {
    filterObj.search = searchDebounced.value.trim()
  }
  if (filters.value.selectedState) {
    filterObj.selected_state = filters.value.selectedState
  }
  
  // Add to pending bulk actions
  pendingBulkActions.value.push({
    action: 'activate',
    filter: Object.keys(filterObj).length > 0 ? filterObj : null,
    count: totalMatching.value
  })
  
  // Update visible tables to show as checked
  for (const table of tables.value) {
    currentActiveState.value.set(table.name, true)
    // Update originalActiveState so subsequent toggles are detected as changes
    originalActiveState.value.set(table.name, true)
  }
}

function deselectAllMatching() {
  // Build filter object matching current filters
  const filterObj: Record<string, any> = {}
  if (selectedSchemas.value.length > 0) {
    filterObj.schema = selectedSchemas.value
  }
  if (selectedConnections.value.length > 0) {
    filterObj.connection = selectedConnections.value
  }
  if (searchDebounced.value.trim()) {
    filterObj.search = searchDebounced.value.trim()
  }
  if (filters.value.selectedState) {
    filterObj.selected_state = filters.value.selectedState
  }
  
  // Add to pending bulk actions
  pendingBulkActions.value.push({
    action: 'deactivate',
    filter: Object.keys(filterObj).length > 0 ? filterObj : null,
    count: totalMatching.value
  })
  
  // Update visible tables to show as unchecked
  for (const table of tables.value) {
    currentActiveState.value.set(table.name, false)
    // Update originalActiveState so subsequent toggles are detected as changes
    originalActiveState.value.set(table.name, false)
  }
}

// Save - executes bulk actions first, then individual delta
async function onSave() {
  if (saving.value) return
  if (!hasPendingChanges.value) { emit('saved', tables.value); return }
  saving.value = true
  
  try {
    // 1. Execute pending bulk actions first (fail fast if any error)
    for (const bulkAction of pendingBulkActions.value) {
      const res = await useMyFetch(`/data_sources/${props.dsId}/bulk_update_tables`, {
        method: 'POST',
        body: {
          action: bulkAction.action,
          filter: bulkAction.filter
        }
      })
      if ((res as any)?.status?.value !== 'success') {
        const errorMsg = `Bulk ${bulkAction.action} failed`
        console.error(errorMsg, bulkAction)
        throw new Error(errorMsg)
      }
    }
    
    // 2. Execute individual delta changes (for single checkbox toggles)
    const toActivate: string[] = []
    const toDeactivate: string[] = []
    
    for (const [name, currentVal] of currentActiveState.value) {
      const originalVal = originalActiveState.value.get(name)
      if (originalVal !== currentVal) {
        if (currentVal) {
          toActivate.push(name)
        } else {
          toDeactivate.push(name)
        }
      }
    }
    
    if (toActivate.length > 0 || toDeactivate.length > 0) {
      await useMyFetch(`/data_sources/${props.dsId}/update_tables_status`, {
        method: 'PUT',
        body: {
          activate: toActivate,
          deactivate: toDeactivate
        }
      })
    }
    
    // 3. Clear all tracking and refresh to get actual state
    pendingBulkActions.value = []
    originalActiveState.value.clear()
    currentActiveState.value.clear()
    if (!props.skipRefreshOnSave) {
      await fetchTables()
    }

    toast.add({
      title: 'Tables updated',
      description: 'Table selection saved successfully',
      color: 'green'
    })
    emit('saved', tables.value)
  } catch (e: any) {
    const errorMsg = e?.message || 'Failed to save table selection'
    toast.add({
      title: 'Save failed',
      description: errorMsg,
      color: 'red'
    })
    emit('error', e)
  } finally {
    saving.value = false
  }
}

async function onRefresh() {
  if (loading.value || refreshing.value) return
  refreshing.value = true

  try {
    if (endpointForSchema() === 'full_schema') {
      await useMyFetch(`/data_sources/${props.dsId}/refresh_schema`, { method: 'GET' })
    }

    // Clear all tracking on refresh
    pendingBulkActions.value = []
    originalActiveState.value.clear()
    currentActiveState.value.clear()
    selectedConnections.value = []
    page.value = 1

    await fetchTables()
  } catch (e) {
    // Swallow refresh errors
  } finally {
    refreshing.value = false
  }
}

// Lifecycle
watch(() => [props.dsId, props.schema], () => {
  if (props.dsId) {
    // Reset all state on datasource change
    page.value = 1
    searchInput.value = ''
    searchDebounced.value = ''
    selectedSchemas.value = []
    selectedConnections.value = []
    filters.value.selectedState = null
    pendingBulkActions.value = []
    originalActiveState.value.clear()
    currentActiveState.value.clear()
    fetchTables()
  }
}, { immediate: true })

onMounted(() => {
  document.addEventListener('click', onGlobalClick)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onGlobalClick)
  if (searchTimeout) clearTimeout(searchTimeout)
})
</script>

<style scoped>
</style>
