/**
 * Domain selection composable.
 * Manages which domains (data sources) are currently selected/filtered.
 * Selection is persisted to localStorage so it survives page refreshes.
 */

interface Domain {
  id: string
  name: string
  type?: string
  description?: string
  connection?: {
    id: string
    name: string
    type: string
    table_count?: number
  }
}

// Storage key for persisting domain selection
const STORAGE_KEY = 'bow_selected_domains'

// Load saved selection from localStorage
function loadFromStorage(): string[] {
  if (typeof window === 'undefined') return []
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored ? JSON.parse(stored) : []
  } catch {
    return []
  }
}

// Save selection to localStorage
function saveToStorage(domainIds: string[]) {
  if (typeof window === 'undefined') return
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(domainIds))
  } catch (e) {
    console.warn('Failed to save domain selection:', e)
  }
}

// Global state (shared across components)
// Initialize from localStorage if available
const selectedDomains = ref<string[]>(loadFromStorage())
const domains = ref<Domain[]>([])
const loading = ref(false)
let watcherInitialized = false

export function useDomain() {
  // Set up watcher to persist selection changes (only once)
  if (!watcherInitialized && typeof window !== 'undefined') {
    watch(selectedDomains, (newSelection) => {
      saveToStorage(newSelection)
    }, { deep: true })
    watcherInitialized = true
  }

  // Computed: check if there are any domains
  const hasDomains = computed(() => domains.value.length > 0)

  // Computed: count of selected domains
  const selectedCount = computed(() => selectedDomains.value.length)

  // Computed: whether "All Domains" is effectively selected (no specific selection)
  const isAllDomains = computed(() => selectedDomains.value.length === 0)

  // Computed: get the current domain name (for display)
  const currentDomainName = computed(() => {
    if (selectedDomains.value.length === 0) {
      return 'All Domains'
    }
    if (selectedDomains.value.length === 1) {
      const domain = domains.value.find(d => d.id === selectedDomains.value[0])
      return domain?.name || 'Selected Domain'
    }
    // Show first 2 domain names comma-separated, then +N for the rest
    const selectedObjs = domains.value.filter(d => selectedDomains.value.includes(d.id))
    const first2 = selectedObjs.slice(0, 2).map(d => d.name)
    const remaining = selectedObjs.length - 2
    if (remaining > 0) {
      return `${first2.join(', ')} +${remaining}`
    }
    return first2.join(', ')
  })

  // Computed: get the selected domain objects
  const selectedDomainObjects = computed(() => {
    if (selectedDomains.value.length === 0) {
      return domains.value // All domains when none selected
    }
    return domains.value.filter(d => selectedDomains.value.includes(d.id))
  })

  // Toggle domain selection
  function toggleDomain(domainId: string | null) {
    if (domainId === null) {
      // "All Domains" selected - clear selection
      selectedDomains.value = []
      return
    }

    const index = selectedDomains.value.indexOf(domainId)
    if (index === -1) {
      // Add domain to selection
      selectedDomains.value = [...selectedDomains.value, domainId]
    } else {
      // Remove domain from selection
      selectedDomains.value = selectedDomains.value.filter(id => id !== domainId)
    }
  }

  // Check if a domain is selected
  function isDomainSelected(domainId: string): boolean {
    // If nothing is selected, all are considered selected
    if (selectedDomains.value.length === 0) {
      return false // Show as not individually selected when "All" is active
    }
    return selectedDomains.value.includes(domainId)
  }

  // Initialize domains by fetching from API
  async function initDomain() {
    loading.value = true
    try {
      const { data } = await useMyFetch<Domain[]>('/data_sources', { method: 'GET' })
      if (data.value) {
        domains.value = data.value
      }
    } catch (error) {
      console.error('Failed to fetch domains:', error)
    } finally {
      loading.value = false
    }
  }

  // Set domains directly (for external initialization)
  function setDomains(newDomains: Domain[]) {
    domains.value = newDomains
  }

  // Clear selection
  function clearSelection() {
    selectedDomains.value = []
  }

  // Select specific domains
  function selectDomains(domainIds: string[]) {
    selectedDomains.value = domainIds
  }

  return {
    // State
    selectedDomains: readonly(selectedDomains),
    domains: readonly(domains),
    loading: readonly(loading),
    
    // Computed
    hasDomains,
    selectedCount,
    isAllDomains,
    currentDomainName,
    selectedDomainObjects,
    
    // Methods
    toggleDomain,
    isDomainSelected,
    initDomain,
    setDomains,
    clearSelection,
    selectDomains,
  }
}
