// /composables/useDomain.ts

export interface Domain {
  id: string
  name: string
  icon?: string
  color?: string
  description?: string
}

const STORAGE_KEY = 'bow_selected_domains'

export const useDomain = () => {
  // Selected domains - empty array means "All Domains"
  const selectedDomains = useState<string[]>('selected_domains', () => [])
  
  // Available domains list
  const domains = useState<Domain[]>('domains_list', () => [])
  
  // Loading state
  const isLoading = useState<boolean>('domains_loading', () => false)

  // Fetch domains from API (using data sources as domains for now)
  const fetchDomains = async () => {
    isLoading.value = true
    try {
      const { data, error } = await useMyFetch<any[]>('/data_sources', { method: 'GET' })
      if (!error.value && data.value) {
        // Map data sources to domain format
        domains.value = (data.value || []).map((ds: any) => ({
          id: ds.id,
          name: ds.name || ds.display_name || 'Unnamed',
          icon: ds.icon || null,
          color: ds.color || null,
          description: ds.description || null
        }))
      }
    } catch (e) {
      console.error('Failed to fetch domains:', e)
    } finally {
      isLoading.value = false
    }
    return domains.value
  }

  // Toggle domain selection
  const toggleDomain = (domainId: string | null) => {
    if (!domainId) {
      // "All Domains" clears selection
      selectedDomains.value = []
      if (typeof window !== 'undefined') {
        localStorage.removeItem(STORAGE_KEY)
      }
      return
    }
    
    const index = selectedDomains.value.indexOf(domainId)
    if (index === -1) {
      // Add domain
      selectedDomains.value = [...selectedDomains.value, domainId]
    } else {
      // Remove domain
      selectedDomains.value = selectedDomains.value.filter(id => id !== domainId)
    }
    
    if (typeof window !== 'undefined') {
      if (selectedDomains.value.length === 0) {
        localStorage.removeItem(STORAGE_KEY)
      } else {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(selectedDomains.value))
      }
    }
  }

  // Check if domain is selected
  const isDomainSelected = (domainId: string) => {
    return selectedDomains.value.includes(domainId)
  }

  // Initialize domains from localStorage
  const initDomain = async () => {
    await fetchDomains()
    
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem(STORAGE_KEY)
      if (saved) {
        try {
          const ids = JSON.parse(saved)
          if (Array.isArray(ids)) {
            // Only keep IDs that still exist
            selectedDomains.value = ids.filter((id: string) => 
              domains.value.some(d => d.id === id)
            )
            if (selectedDomains.value.length > 0) {
              localStorage.setItem(STORAGE_KEY, JSON.stringify(selectedDomains.value))
            } else {
              localStorage.removeItem(STORAGE_KEY)
            }
          }
        } catch (e) {
          // Invalid JSON, clear it
          localStorage.removeItem(STORAGE_KEY)
        }
      }
    }
  }

  // Clear all domain selections
  const clearDomain = () => {
    selectedDomains.value = []
    if (typeof window !== 'undefined') {
      localStorage.removeItem(STORAGE_KEY)
    }
  }

  // Computed helpers
  const hasDomains = computed(() => domains.value.length > 0)
  const selectedCount = computed(() => selectedDomains.value.length)
  const isAllDomains = computed(() => selectedDomains.value.length === 0)
  
  // Display name for selected domains
  const currentDomainName = computed(() => {
    if (selectedDomains.value.length === 0) return 'All Domains'
    if (selectedDomains.value.length === 1) {
      const domain = domains.value.find(d => d.id === selectedDomains.value[0])
      return domain?.name || 'Selected'
    }
    return `${selectedDomains.value.length} domains`
  })
  
  // Get selected domain objects
  const selectedDomainObjects = computed(() => {
    return domains.value.filter(d => selectedDomains.value.includes(d.id))
  })

  return {
    selectedDomains,
    domains,
    isLoading,
    hasDomains,
    selectedCount,
    isAllDomains,
    currentDomainName,
    selectedDomainObjects,
    fetchDomains,
    toggleDomain,
    isDomainSelected,
    initDomain,
    clearDomain
  }
}

