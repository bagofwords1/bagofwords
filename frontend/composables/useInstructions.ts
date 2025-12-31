/**
 * Composable for fetching and managing instructions with server-side pagination
 */
import type { Instruction } from './useInstructionHelpers'

export interface UseInstructionsOptions {
  dataSourceId?: string | Ref<string | undefined>
  dataSourceIds?: string[] | Ref<string[] | undefined>  // Support multiple domain IDs
  autoFetch?: boolean
  pageSize?: number
  persistFiltersInUrl?: boolean
  onBulkSuccess?: () => void | Promise<void>  // Callback after bulk operations
}

export interface InstructionFilters {
  search: string
  status: string | null
  categories: string[]
  sourceTypes: string[]
  loadModes: string[]
  labelIds: string[]
  dataSourceId: string | null  // Single ID (deprecated, for backward compat)
  dataSourceIds: string[]  // Multiple domain IDs
  buildId: string | null
  includeGlobal: boolean  // Include global instructions when filtering by data sources
}

export interface PaginatedResponse {
  items: Instruction[]
  total: number
  page: number
  per_page: number
  pages: number
}

export function useInstructions(options: UseInstructionsOptions = {}) {
  const {
    dataSourceId,
    dataSourceIds,
    autoFetch = true,
    pageSize = 25,
    persistFiltersInUrl = false,
    onBulkSuccess
  } = options

  const route = useRoute()
  const router = useRouter()
  const toast = useToast()

  // State
  const instructions = ref<Instruction[]>([])
  const isLoading = ref(false)
  const isBulkUpdating = ref(false)
  const error = ref<string | null>(null)
  const total = ref(0)
  const pages = ref(1)
  
  // Pagination
  const currentPage = ref(1)
  const itemsPerPage = ref(pageSize)

  // Filters - server-side
  const filters = reactive<InstructionFilters>({
    search: '',
    status: null,
    categories: [],
    sourceTypes: [],
    loadModes: [],
    labelIds: [],
    dataSourceId: null,
    dataSourceIds: [],
    buildId: null,
    includeGlobal: true  // Default to including global instructions
  })

  // Selection state
  const selectedIds = ref<Set<string>>(new Set())
  const selectAllMode = ref<'none' | 'page' | 'all'>('none')

  // Computed: resolved data source IDs (supports both single and multiple)
  const resolvedDataSourceIds = computed((): string[] => {
    // Priority: filter.dataSourceIds > filter.dataSourceId > option.dataSourceIds > option.dataSourceId
    if (filters.dataSourceIds.length > 0) return filters.dataSourceIds
    if (filters.dataSourceId) return [filters.dataSourceId]
    if (dataSourceIds) {
      const ids = isRef(dataSourceIds) ? dataSourceIds.value : dataSourceIds
      if (ids && ids.length > 0) return ids
    }
    if (dataSourceId) {
      const id = isRef(dataSourceId) ? dataSourceId.value : dataSourceId
      if (id) return [id]
    }
    return []
  })
  
  // Backward compat: single resolved ID
  const resolvedDataSourceId = computed(() => resolvedDataSourceIds.value[0] || undefined)

  // Computed: all selected (for current page)
  const isAllPageSelected = computed(() => {
    if (instructions.value.length === 0) return false
    return instructions.value.every(i => selectedIds.value.has(i.id))
  })

  // Computed: some selected (for current page)
  const isSomeSelected = computed(() => {
    return instructions.value.some(i => selectedIds.value.has(i.id))
  })

  // Computed: selection count
  const selectedCount = computed(() => {
    if (selectAllMode.value === 'all') return total.value
    return selectedIds.value.size
  })

  // Computed: visible page numbers
  const visiblePages = computed(() => {
    const result: number[] = []
    const totalPages = pages.value
    const current = currentPage.value
    let start = Math.max(1, current - 2)
    let end = Math.min(totalPages, start + 4)
    if (end - start < 4) start = Math.max(1, end - 4)
    for (let i = start; i <= end; i++) result.push(i)
    return result
  })

  // Initialize filters from URL if persistFiltersInUrl
  const initFromUrl = () => {
    if (!persistFiltersInUrl) return
    const q = route.query
    if (q.search) filters.search = String(q.search)
    if (q.status) filters.status = String(q.status)
    if (q.categories) filters.categories = String(q.categories).split(',').filter(Boolean)
    if (q.source_types) filters.sourceTypes = String(q.source_types).split(',').filter(Boolean)
    if (q.load_modes) filters.loadModes = String(q.load_modes).split(',').filter(Boolean)
    if (q.label_ids) filters.labelIds = String(q.label_ids).split(',').filter(Boolean)
    if (q.page) currentPage.value = parseInt(String(q.page)) || 1
  }

  // Update URL with current filters
  const updateUrl = () => {
    if (!persistFiltersInUrl) return
    const query: Record<string, string> = {}
    if (filters.search) query.search = filters.search
    if (filters.status) query.status = filters.status
    if (filters.categories.length) query.categories = filters.categories.join(',')
    if (filters.sourceTypes.length) query.source_types = filters.sourceTypes.join(',')
    if (filters.loadModes.length) query.load_modes = filters.loadModes.join(',')
    if (filters.labelIds.length) query.label_ids = filters.labelIds.join(',')
    if (currentPage.value > 1) query.page = String(currentPage.value)
    router.replace({ query })
  }

  // Fetch instructions from server
  const fetchInstructions = async () => {
    isLoading.value = true
    error.value = null

    try {
      const queryParams: Record<string, any> = {
        skip: (currentPage.value - 1) * itemsPerPage.value,
        limit: itemsPerPage.value,
        include_own: true,
        include_drafts: true,
        include_archived: filters.status === 'archived'
      }

      // Add filters - use comma-separated IDs for domain filtering
      if (resolvedDataSourceIds.value.length > 0) queryParams.data_source_ids = resolvedDataSourceIds.value.join(',')
      if (filters.status) queryParams.status = filters.status
      if (filters.categories.length) queryParams.categories = filters.categories.join(',')
      if (filters.sourceTypes.length) queryParams.source_types = filters.sourceTypes.join(',')
      if (filters.loadModes.length) queryParams.load_modes = filters.loadModes.join(',')
      if (filters.labelIds.length) queryParams.label_ids = filters.labelIds.join(',')
      if (filters.search?.trim()) queryParams.search = filters.search.trim()
      if (filters.buildId) queryParams.build_id = filters.buildId
      // Include global instructions flag (default true on backend, only pass if false)
      if (!filters.includeGlobal) queryParams.include_global = false

      const { data, error: fetchError } = await useMyFetch<PaginatedResponse>('/api/instructions', {
        method: 'GET',
        query: queryParams
      })

      if (fetchError.value) {
        throw new Error(fetchError.value.message || 'Failed to fetch instructions')
      }

      const response = data.value as PaginatedResponse
      instructions.value = response?.items || []
      total.value = response?.total || 0
      pages.value = response?.pages || 1

      // Clamp page if out of bounds
      if (currentPage.value > pages.value && pages.value > 0) {
        currentPage.value = pages.value
      }

      updateUrl()
    } catch (err: any) {
      error.value = err.message || 'Failed to fetch instructions'
      console.error('Error fetching instructions:', err)
    } finally {
      isLoading.value = false
    }
  }

  // Debounced search
  let searchTimeout: ReturnType<typeof setTimeout> | null = null
  const debouncedSearch = (value: string) => {
    filters.search = value
    if (searchTimeout) clearTimeout(searchTimeout)
    searchTimeout = setTimeout(() => {
      currentPage.value = 1
      fetchInstructions()
    }, 300)
  }

  // Set page
  const setPage = (page: number) => {
    currentPage.value = Math.max(1, Math.min(page, pages.value))
    fetchInstructions()
  }

  // Set filter and refetch
  const setFilter = <K extends keyof InstructionFilters>(key: K, value: InstructionFilters[K]) => {
    filters[key] = value
    currentPage.value = 1
    fetchInstructions()
  }

  // Reset filters
  const resetFilters = () => {
    filters.search = ''
    filters.status = null
    filters.categories = []
    filters.sourceTypes = []
    filters.loadModes = []
    filters.labelIds = []
    currentPage.value = 1
    fetchInstructions()
  }

  // Refresh
  const refresh = () => fetchInstructions()

  // Selection methods
  const toggleSelection = (id: string) => {
    selectAllMode.value = 'none'
    if (selectedIds.value.has(id)) {
      selectedIds.value.delete(id)
    } else {
      selectedIds.value.add(id)
    }
    selectedIds.value = new Set(selectedIds.value)
  }

  const selectPage = () => {
    selectAllMode.value = 'page'
    instructions.value.forEach(i => selectedIds.value.add(i.id))
    selectedIds.value = new Set(selectedIds.value)
  }

  const selectAll = () => {
    selectAllMode.value = 'all'
    // In "all" mode, we don't need to track individual IDs
    // The bulk action will use the current filter params instead
  }

  const clearSelection = () => {
    selectedIds.value = new Set()
    selectAllMode.value = 'none'
  }

  const togglePageSelection = () => {
    if (isAllPageSelected.value) {
      // Deselect all on current page
      instructions.value.forEach(i => selectedIds.value.delete(i.id))
      selectedIds.value = new Set(selectedIds.value)
      selectAllMode.value = 'none'
    } else {
      selectPage()
    }
  }

  // Bulk actions
  const bulkUpdate = async (updates: { status?: string; load_mode?: string; add_label_ids?: string[]; remove_label_ids?: string[] }) => {
    isBulkUpdating.value = true
    try {
      // If selectAllMode is 'all', we need to fetch all matching IDs first
      let idsToUpdate: string[] = []
      
      if (selectAllMode.value === 'all') {
        // Fetch all IDs matching current filters
        const queryParams: Record<string, any> = {
          skip: 0,
          limit: 10000, // Get all
          include_own: true,
          include_drafts: true,
          include_archived: filters.status === 'archived'
        }
        if (resolvedDataSourceIds.value.length > 0) queryParams.data_source_ids = resolvedDataSourceIds.value.join(',')
        if (filters.status) queryParams.status = filters.status
        if (filters.categories.length) queryParams.categories = filters.categories.join(',')
        if (filters.sourceTypes.length) queryParams.source_types = filters.sourceTypes.join(',')
        if (filters.loadModes.length) queryParams.load_modes = filters.loadModes.join(',')
        if (filters.labelIds.length) queryParams.label_ids = filters.labelIds.join(',')
        if (filters.search?.trim()) queryParams.search = filters.search.trim()

        const { data } = await useMyFetch<PaginatedResponse>('/api/instructions', {
          method: 'GET',
          query: queryParams
        })
        idsToUpdate = (data.value?.items || []).map((i: Instruction) => i.id)
      } else {
        idsToUpdate = Array.from(selectedIds.value)
      }

      if (idsToUpdate.length === 0) {
        toast.add({ title: 'No instructions selected', color: 'yellow' })
        return
      }

      const { data, error: updateError } = await useMyFetch('/api/instructions/bulk', {
        method: 'PUT',
        body: {
          ids: idsToUpdate,
          ...updates
        }
      })

      if (updateError.value) {
        throw new Error(updateError.value.message || 'Bulk update failed')
      }

      const result = data.value as { updated_count: number; message: string }
      toast.add({ 
        title: 'Success', 
        description: result?.message || `Updated ${result?.updated_count} instructions`,
        color: 'green'
      })

      clearSelection()
      await fetchInstructions()
      
      // Trigger callback to refresh builds list
      if (onBulkSuccess) {
        await onBulkSuccess()
      }
    } catch (err: any) {
      toast.add({ title: 'Error', description: err.message, color: 'red' })
    } finally {
      isBulkUpdating.value = false
    }
  }

  // Convenience bulk action methods
  const bulkPublish = () => bulkUpdate({ status: 'published' })
  const bulkArchive = () => bulkUpdate({ status: 'archived' })
  const bulkMakeDraft = () => bulkUpdate({ status: 'draft' })
  const bulkSetLoadAlways = () => bulkUpdate({ load_mode: 'always' })
  const bulkSetLoadIntelligent = () => bulkUpdate({ load_mode: 'intelligent' })
  const bulkSetLoadDisabled = () => bulkUpdate({ load_mode: 'disabled' })
  const bulkAddLabel = (labelId: string) => bulkUpdate({ add_label_ids: [labelId] })
  const bulkRemoveLabel = (labelId: string) => bulkUpdate({ remove_label_ids: [labelId] })

  // Bulk delete
  const bulkDelete = async () => {
    isBulkUpdating.value = true
    try {
      // If selectAllMode is 'all', fetch all matching IDs first
      let idsToDelete: string[] = []
      
      if (selectAllMode.value === 'all') {
        // Fetch all IDs matching current filters
        const queryParams: Record<string, any> = {
          skip: 0,
          limit: 10000,
          include_own: true,
          include_drafts: true,
          include_archived: filters.status === 'archived'
        }
        if (resolvedDataSourceIds.value.length > 0) queryParams.data_source_ids = resolvedDataSourceIds.value.join(',')
        if (filters.status) queryParams.status = filters.status
        if (filters.categories.length) queryParams.categories = filters.categories.join(',')
        if (filters.sourceTypes.length) queryParams.source_types = filters.sourceTypes.join(',')
        if (filters.loadModes.length) queryParams.load_modes = filters.loadModes.join(',')
        if (filters.labelIds.length) queryParams.label_ids = filters.labelIds.join(',')
        if (filters.search?.trim()) queryParams.search = filters.search.trim()

        const { data } = await useMyFetch<PaginatedResponse>('/api/instructions', {
          method: 'GET',
          query: queryParams
        })
        idsToDelete = (data.value?.items || []).map((i: Instruction) => i.id)
      } else {
        idsToDelete = Array.from(selectedIds.value)
      }

      if (idsToDelete.length === 0) {
        toast.add({ title: 'No instructions selected', color: 'yellow' })
        return
      }

      const { data, error: deleteError } = await useMyFetch('/api/instructions/bulk', {
        method: 'DELETE',
        body: { ids: idsToDelete }
      })

      if (deleteError.value) {
        throw new Error(deleteError.value.message || 'Bulk delete failed')
      }

      const result = data.value as { updated_count: number; message: string }
      toast.add({ 
        title: 'Success', 
        description: result?.message || `Deleted ${result?.updated_count} instructions`,
        color: 'green'
      })

      clearSelection()
      await fetchInstructions()
      
      if (onBulkSuccess) {
        await onBulkSuccess()
      }
    } catch (err: any) {
      toast.add({ title: 'Error', description: err.message, color: 'red' })
    } finally {
      isBulkUpdating.value = false
    }
  }

  // Watch for dataSourceId changes
  watch(resolvedDataSourceId, () => {
    currentPage.value = 1
    fetchInstructions()
  })

  // Initialize
  if (persistFiltersInUrl) {
    initFromUrl()
  }

  if (autoFetch) {
    onMounted(() => fetchInstructions())
  }

  return {
    // State
    instructions,
    isLoading,
    isBulkUpdating,
    error,
    total,
    pages,
    
    // Filters
    filters,
    
    // Pagination
    currentPage,
    itemsPerPage,
    visiblePages,
    
    // Selection
    selectedIds,
    selectAllMode,
    selectedCount,
    isAllPageSelected,
    isSomeSelected,
    
    // Methods
    fetchInstructions,
    refresh,
    setPage,
    setFilter,
    resetFilters,
    debouncedSearch,
    
    // Selection methods
    toggleSelection,
    selectPage,
    selectAll,
    clearSelection,
    togglePageSelection,
    
    // Bulk actions
    bulkUpdate,
    bulkPublish,
    bulkArchive,
    bulkMakeDraft,
    bulkSetLoadAlways,
    bulkSetLoadIntelligent,
    bulkSetLoadDisabled,
    bulkAddLabel,
    bulkRemoveLabel,
    bulkDelete
  }
}
