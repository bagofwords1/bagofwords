<template>
  <div class="w-full">
    <div v-if="showHeader" class="mb-2">
      <h1 class="text-lg font-semibold">{{ headerTitle }}</h1>
      <p class="text-gray-500 text-sm">{{ headerSubtitle }}</p>
    </div>

    <div v-if="!loading && totalResources > 0" class="space-y-2">
      <!-- Row 1: Search + Filter/Sort -->
      <div class="relative flex items-center gap-2">
        <div class="relative flex-1">
          <UIcon name="heroicons:magnifying-glass" class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input v-model="resourceSearch" type="text" :placeholder="viewMode === 'files' ? 'Search files...' : 'Search resources...'" class="border border-gray-300 rounded-lg pl-9 pr-3 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
        </div>
        <button
          ref="filterButtonRef"
          type="button"
          @click="toggleFilterMenu"
          class="h-9 w-9 inline-flex items-center justify-center rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50"
          aria-label="Filter resources"
        >
          <UIcon name="heroicons-funnel" class="w-5 h-5" />
        </button>
        <button
          ref="sortButtonRef"
          type="button"
          @click="toggleSortMenu"
          class="h-9 w-9 inline-flex items-center justify-center rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50"
          aria-label="Sort resources"
        >
          <UIcon name="heroicons-arrows-up-down" class="w-5 h-5" />
        </button>
        <!-- Filter dropdown -->
        <div
          v-if="filterMenuOpen"
          ref="filterMenuRef"
          class="absolute right-0 top-full mt-1 z-10 bg-white border border-gray-200 rounded-lg shadow-md w-40"
        >
          <div class="py-1">
            <button
              type="button"
              class="w-full text-left px-3 py-1.5 text-sm hover:bg-gray-50 flex items-center justify-between"
              @click="setSelectedFilter('selected')"
            >
              <span>Selected</span>
              <UIcon v-if="filters.selectedState === 'selected'" name="heroicons-check" class="w-4 h-4 text-blue-600" />
            </button>
            <button
              type="button"
              class="w-full text-left px-3 py-1.5 text-sm hover:bg-gray-50 flex items-center justify-between"
              @click="setSelectedFilter('unselected')"
            >
              <span>Unselected</span>
              <UIcon v-if="filters.selectedState === 'unselected'" name="heroicons-check" class="w-4 h-4 text-blue-600" />
            </button>
          </div>
        </div>
        <!-- Sort dropdown -->
        <div
          v-if="sortMenuOpen"
          ref="sortMenuRef"
          class="absolute right-0 top-full mt-1 z-10 bg-white border border-gray-200 rounded-lg shadow-md w-40"
        >
          <div class="py-1">
            <button
              type="button"
              class="w-full text-left px-3 py-1.5 text-sm hover:bg-gray-50 flex items-center justify-between"
              @click="setSort('name')"
            >
              <span>Name</span>
              <UIcon v-if="sort.key === 'name'" name="heroicons-check" class="w-4 h-4 text-blue-600" />
            </button>
            <button
              type="button"
              class="w-full text-left px-3 py-1.5 text-sm hover:bg-gray-50 flex items-center justify-between"
              @click="setSort('type')"
            >
              <span>Type</span>
              <UIcon v-if="sort.key === 'type'" name="heroicons-check" class="w-4 h-4 text-blue-600" />
            </button>
          </div>
        </div>
      </div>

      <!-- Row 2: View toggle + Count + Bulk actions -->
      <div class="flex items-center justify-between mb-4 mt-4">
        <!-- View mode toggle -->
        <UTooltip text="Toggle between individual chunks and grouped files">
          <div class="flex items-center gap-1.5 h-8 px-2 border border-gray-200 rounded-lg bg-gray-50">
            <span :class="['text-xs font-medium', viewMode === 'chunks' ? 'text-gray-700' : 'text-gray-400']">Chunks</span>
            <UToggle
              :model-value="viewMode === 'files'"
              @update:model-value="viewMode = $event ? 'files' : 'chunks'"
              color="blue"
              size="xs"
            />
            <span :class="['text-xs font-medium', viewMode === 'files' ? 'text-gray-700' : 'text-gray-400']">Files</span>
          </div>
        </UTooltip>
        <!-- Selection count + Bulk actions -->
        <div class="flex items-center gap-3">
          <span class="text-xs text-gray-500">
            <span class="font-medium text-gray-700">{{ selectedCount }}</span> of {{ totalResources }} selected
          </span>
          <div v-if="canUpdate" class="flex items-center gap-1.5">
            <button
              @click="selectAll"
              :disabled="loading || saving"
              class="px-2 py-1 text-xs rounded border border-gray-300 text-gray-600 hover:bg-gray-50 disabled:opacity-50"
            >
              Select all
            </button>
            <button
              @click="deselectAll"
              :disabled="loading || saving"
              class="px-2 py-1 text-xs rounded border border-gray-300 text-gray-600 hover:bg-gray-50 disabled:opacity-50"
            >
              Deselect all
            </button>
          </div>
        </div>
      </div>
    </div>

    <div v-if="loading" class="text-sm text-gray-500 py-10 flex items-center justify-center">
      <Spinner class="w-4 h-4 mr-2" />
      Loading resources...
    </div>

    <div v-else class="flex-1 flex flex-col h-full mt-4">
      <!-- Files view -->
      <template v-if="viewMode === 'files'">
        <div v-if="filteredFiles.length === 0" class="text-sm text-gray-500">No files found.</div>
        <div v-else class="flex-1 flex flex-col min-h-full">
          <div class="flex-1 overflow-y-auto min-h-0 border border-gray-100 rounded" :style="{ maxHeight }">
            <ul class="divide-y divide-gray-100">
              <li v-for="file in filteredFiles" :key="file.path" class="py-2 px-3">
                <div class="flex items-center">
                  <UCheckbox
                    v-if="canUpdate"
                    :model-value="file.isActive === true"
                    :indeterminate="file.isActive === null"
                    @update:model-value="toggleFileActive(file)"
                    color="blue"
                    class="mr-2"
                  />
                  <div class="flex-1 cursor-pointer flex items-center" @click="toggleFileExpanded(file)">
                    <UIcon :name="expandedFiles[file.path] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 mr-1 text-gray-400" />
                    <UIcon name="heroicons:document-text" class="w-4 h-4 text-gray-500 mr-2" />
                    <span class="text-sm font-medium text-gray-700">{{ file.displayName }}</span>
                    <span class="ml-2 text-xs text-gray-400">({{ file.resources.length }} {{ file.resources.length === 1 ? 'chunk' : 'chunks' }})</span>
                  </div>
                </div>
                <!-- Expanded file shows its chunks -->
                <div v-if="expandedFiles[file.path]" class="ml-6 mt-2 space-y-1">
                  <div v-for="res in file.resources" :key="res.id" class="flex items-center py-1 px-2 rounded hover:bg-gray-50">
                    <UCheckbox v-if="canUpdate" v-model="res.is_active" color="blue" class="mr-2" />
                    <UIcon v-if="res.resource_type === 'model' || res.resource_type === 'model_config'" name="heroicons:cube" class="w-3 h-3 text-gray-400 mr-1" />
                    <UIcon v-else-if="res.resource_type === 'metric'" name="heroicons:hashtag" class="w-3 h-3 text-gray-400 mr-1" />
                    <UIcon v-else name="heroicons:puzzle-piece" class="w-3 h-3 text-gray-400 mr-1" />
                    <span class="text-xs text-gray-600">{{ res.name }}</span>
                  </div>
                </div>
              </li>
            </ul>
          </div>
        </div>
      </template>

      <!-- Chunks view (original) -->
      <template v-else>
        <div v-if="filteredResources.length === 0" class="text-sm text-gray-500">No resources found.</div>
        <div v-else class="flex-1 flex flex-col min-h-full">
          <div class="flex-1 overflow-y-auto min-h-0 border border-gray-100 rounded" :style="{ maxHeight }">
            <ul class="divide-y divide-gray-100">
              <li v-for="res in filteredResources" :key="res.id" class="py-2 px-3">
                <div class="flex items-center">
                  <UCheckbox v-if="canUpdate" v-model="res.is_active" color="blue" class="mr-2" />
                  <div class="font-semibold text-gray-600 cursor-pointer flex items-center" @click="toggleResource(res)">
                    <UIcon :name="expandedResources[res.id] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 mr-1" />
                    <UIcon v-if="res.resource_type === 'model' || res.resource_type === 'model_config'" name="heroicons:cube" class="w-4 h-4 text-gray-500 mr-1" />
                    <UIcon v-else-if="res.resource_type === 'metric'" name="heroicons:hashtag" class="w-4 h-4 text-gray-500 mr-1" />
                    <span class="text-sm">{{ res.name }}</span>
                  </div>
                </div>
                <div v-if="expandedResources[res.id]" class="ml-6 mt-2">
                  <ResourceDisplay :resource="res" />
                </div>
              </li>
            </ul>
          </div>
        </div>
      </template>
    </div>

    <div v-if="showSave && canUpdate && totalResources > 0" class="mt-3 flex justify-end">
      <button @click="onSave" :disabled="saving" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
        <span v-if="saving">Saving...</span>
        <span v-else>{{ saveLabel }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '@/components/Spinner.vue'
import ResourceDisplay from '~/components/ResourceDisplay.vue'

type MetadataResource = Record<string, any> & {
  id: string
  name: string
  path?: string
  resource_type: string
  is_active?: boolean
}

type FileGroup = {
  path: string
  displayName: string
  resources: MetadataResource[]
  isActive: boolean | null // true = all active, false = all inactive, null = mixed
}

const props = withDefaults(defineProps<{ 
  dsId: string
  canUpdate?: boolean
  showRefresh?: boolean
  refreshIconOnly?: boolean
  showSave?: boolean
  saveLabel?: string
  maxHeight?: string
  showHeader?: boolean
  headerTitle?: string
  headerSubtitle?: string
}>(), { canUpdate: true, showRefresh: true, refreshIconOnly: false, showSave: true, saveLabel: 'Save resources', maxHeight: '50vh', showHeader: false, headerTitle: 'Resources', headerSubtitle: 'Toggle which resources to enable' })

const emit = defineEmits<{ (e: 'saved', resources: MetadataResource[]): void; (e: 'error', err: any): void }>()

const loading = ref(false)
const saving = ref(false)
const resources = ref<MetadataResource[]>([])
const resourceSearch = ref('')
const expandedResources = ref<Record<string, boolean>>({})
const viewMode = ref<'chunks' | 'files'>('chunks')
const expandedFiles = ref<Record<string, boolean>>({})

const filterMenuOpen = ref(false)
const filterMenuRef = ref<HTMLElement | null>(null)
const filterButtonRef = ref<HTMLElement | null>(null)
const filters = ref<{ selectedState: 'selected' | 'unselected' | null }>({
  selectedState: null
})
const sortMenuOpen = ref(false)
const sortMenuRef = ref<HTMLElement | null>(null)
const sortButtonRef = ref<HTMLElement | null>(null)
const sort = reactive<{ key: 'name' | 'type' | null; direction: 'asc' | 'desc' }>({
  key: 'name',
  direction: 'asc'
})

const totalResources = computed(() => (resources.value || []).length)
const selectedCount = computed(() => (resources.value || []).filter(r => !!r.is_active).length)
const visibleResources = computed(() => {
  let list = resources.value || []
  if (!props.canUpdate) {
    list = list.filter(r => !!r.is_active)
  }
  return list
})
const filteredResources = computed(() => {
  const q = resourceSearch.value.trim().toLowerCase()
  let list = visibleResources.value
  // Selection filter
  if (filters.value.selectedState === 'selected') {
    list = list.filter(r => !!r.is_active)
  } else if (filters.value.selectedState === 'unselected') {
    list = list.filter(r => !r.is_active)
  }
  // Search
  if (q) {
    list = list.filter((r: any) => String(r.name || '').toLowerCase().includes(q))
  }
  // Sorting
  if (sort.key) {
    const dir = sort.direction === 'asc' ? 1 : -1
    list = [...list].sort((a, b) => {
      if (sort.key === 'name') {
        return String(a.name).localeCompare(String(b.name)) * dir
      }
      // type
      return String(a.resource_type || '').localeCompare(String(b.resource_type || '')) * dir
    })
  }
  return list
})

// Group resources by file path
const fileGroupedResources = computed((): FileGroup[] => {
  const byPath = new Map<string, MetadataResource[]>()
  
  for (const res of visibleResources.value) {
    const path = res.path || '(no path)'
    if (!byPath.has(path)) {
      byPath.set(path, [])
    }
    byPath.get(path)!.push(res)
  }
  
  const groups: FileGroup[] = []
  for (const [path, pathResources] of byPath.entries()) {
    const activeCount = pathResources.filter(r => !!r.is_active).length
    const isActive = activeCount === 0 ? false : activeCount === pathResources.length ? true : null
    
    // Extract display name from path (last part)
    const displayName = path.split('/').pop() || path
    
    groups.push({
      path,
      displayName,
      resources: pathResources,
      isActive,
    })
  }
  
  return groups
})

const totalFiles = computed(() => fileGroupedResources.value.length)

const filteredFiles = computed((): FileGroup[] => {
  const q = resourceSearch.value.trim().toLowerCase()
  let list = fileGroupedResources.value
  
  // Selection filter
  if (filters.value.selectedState === 'selected') {
    list = list.filter(f => f.isActive === true)
  } else if (filters.value.selectedState === 'unselected') {
    list = list.filter(f => f.isActive === false)
  }
  
  // Search by path or display name
  if (q) {
    list = list.filter(f => 
      f.path.toLowerCase().includes(q) || 
      f.displayName.toLowerCase().includes(q)
    )
  }
  
  // Sorting
  if (sort.key) {
    const dir = sort.direction === 'asc' ? 1 : -1
    list = [...list].sort((a, b) => {
      if (sort.key === 'name') {
        return a.path.localeCompare(b.path) * dir
      }
      // For type sorting in file view, use first resource's type
      const aType = a.resources[0]?.resource_type || ''
      const bType = b.resources[0]?.resource_type || ''
      return aType.localeCompare(bType) * dir
    })
  }
  
  return list
})

function toggleResource(res: MetadataResource) {
  expandedResources.value[res.id] = !expandedResources.value[res.id]
}

function toggleFileExpanded(file: FileGroup) {
  expandedFiles.value[file.path] = !expandedFiles.value[file.path]
}

function toggleFileActive(file: FileGroup) {
  // If mixed or all inactive, set all to active; if all active, set all to inactive
  const newState = file.isActive !== true
  for (const res of file.resources) {
    res.is_active = newState
  }
}

function selectAll() {
  if (viewMode.value === 'files') {
    // In file mode, select all resources in filtered files
    for (const file of filteredFiles.value) {
      for (const res of file.resources) {
        res.is_active = true
      }
    }
  } else {
    // In chunks mode, select filtered resources
    for (const res of filteredResources.value) {
      res.is_active = true
    }
  }
}

function deselectAll() {
  if (viewMode.value === 'files') {
    // In file mode, deselect all resources in filtered files
    for (const file of filteredFiles.value) {
      for (const res of file.resources) {
        res.is_active = false
      }
    }
  } else {
    // In chunks mode, deselect filtered resources
    for (const res of filteredResources.value) {
      res.is_active = false
    }
  }
}

function toggleFilterMenu() {
  filterMenuOpen.value = !filterMenuOpen.value
}

function setSelectedFilter(state: 'selected' | 'unselected') {
  filters.value.selectedState = filters.value.selectedState === state ? null : state
  filterMenuOpen.value = false
}

function toggleSortMenu() {
  sortMenuOpen.value = !sortMenuOpen.value
}

function setSort(key: 'name' | 'type') {
  if (sort.key === key) {
    // toggle direction if same key selected
    sort.direction = sort.direction === 'asc' ? 'desc' : 'asc'
  } else {
    sort.key = key
    // default directions: name asc, type asc
    sort.direction = 'asc'
  }
  sortMenuOpen.value = false
}

function onGlobalClick(e: MouseEvent) {
  const target = e.target as Node
  // Close filter menu if click is outside
  if (filterMenuOpen.value) {
    const insideFilter = (filterMenuRef.value && filterMenuRef.value.contains(target)) || (filterButtonRef.value && filterButtonRef.value.contains(target))
    if (!insideFilter) filterMenuOpen.value = false
  }
  // Close sort menu if click is outside
  if (sortMenuOpen.value) {
    const insideSort = (sortMenuRef.value && sortMenuRef.value.contains(target)) || (sortButtonRef.value && sortButtonRef.value.contains(target))
    if (!insideSort) sortMenuOpen.value = false
  }
}

async function fetchResources() {
  if (!props.dsId) return
  loading.value = true
  try {
    // Try paginated fetching until all items are returned.
    const pageSize = 200
    let skip = 0
    let total: number | null = null
    const aggregated: any[] = []
    let usedNonPaginated = false

    while (true) {
      const res = await useMyFetch(`/data_sources/${props.dsId}/metadata_resources`, {
        method: 'GET',
        params: { skip, limit: pageSize }
      })
      const payload: any = (res as any)?.data?.value ?? {}

      // Handle older/non-paginated shape { resources: [...] }
      if (Array.isArray(payload?.resources)) {
        resources.value = (payload.resources as any[]).map((r) => ({ resource_type: '', ...r })) as MetadataResource[]
        usedNonPaginated = true
        break
      }

      // Handle paginated shape { items: [...], total: number }
      const items: any[] = Array.isArray(payload?.items) ? payload.items : []
      if (typeof payload?.total === 'number') {
        total = payload.total
      }
      aggregated.push(...items)

      if (!items.length || (total !== null && aggregated.length >= total)) {
        break
      }
      skip += items.length
    }

    if (!usedNonPaginated) {
      resources.value = aggregated.map((r) => ({ resource_type: '', ...r })) as MetadataResource[]
    }
  } catch (e) {
    emit('error', e)
  } finally {
    loading.value = false
  }
}

async function onSave() {
  if (saving.value) return
  saving.value = true
  try {
    const res = await useMyFetch(`/data_sources/${props.dsId}/update_metadata_resources`, { method: 'PUT', body: resources.value })
    const status = (res as any)?.status?.value
    if (status === 'success') {
      const updated = (res as any).data?.value
      if (Array.isArray(updated)) {
        resources.value = updated as MetadataResource[]
        emit('saved', resources.value)
      } else {
        // Response wasn't an array but request succeeded - keep current resources
        emit('saved', resources.value)
      }
    } else if (status === 'error') {
      emit('error', (res as any)?.error?.value || new Error('Failed to save'))
    }
  } catch (e) {
    emit('error', e)
  } finally {
    saving.value = false
  }
}

defineExpose({ refresh: fetchResources })

watch(() => props.dsId, () => { if (props.dsId) fetchResources() }, { immediate: true })

onMounted(() => {
  document.addEventListener('click', onGlobalClick)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onGlobalClick)
})
</script>

<style scoped>
</style>


