/**
 * Agent selection composable.
 * Manages which agents (data sources) are currently selected/filtered.
 * Selection is persisted to localStorage so it survives page refreshes.
 */
import { useCan, useHasOrgWideConsole } from '~/composables/usePermissions'

interface AgentConnection {
  id: string
  name: string
  type: string
  auth_policy?: string
  allowed_user_auth_modes?: string[]
  is_active?: boolean
  last_synced_at?: string
  user_status?: {
    has_user_credentials: boolean
    auth_mode?: string
    is_primary?: boolean
    connection: string
    effective_auth: string
  }
  table_count?: number
}

interface Agent {
  id: string
  name: string
  type?: string  // Legacy field - computed from first connection
  description?: string
  connections: AgentConnection[]  // Now an array of connections
}

// Storage key for persisting agent selection
const STORAGE_KEY = 'bow_selected_agents'
const LEGACY_STORAGE_KEY = 'bow_selected_domains'

// Load saved selection from localStorage
function loadFromStorage(): string[] {
  if (typeof window === 'undefined') return []
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored) return JSON.parse(stored)
    // One-time migration from legacy key so users don't lose their selection.
    const legacy = localStorage.getItem(LEGACY_STORAGE_KEY)
    if (legacy) {
      localStorage.setItem(STORAGE_KEY, legacy)
      localStorage.removeItem(LEGACY_STORAGE_KEY)
      return JSON.parse(legacy)
    }
    return []
  } catch {
    return []
  }
}

// Save selection to localStorage
function saveToStorage(agentIds: string[]) {
  if (typeof window === 'undefined') return
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(agentIds))
  } catch (e) {
    console.warn('Failed to save agent selection:', e)
  }
}

// Global state (shared across components)
// Initialize from localStorage if available
const selectedAgents = ref<string[]>(loadFromStorage())
const agents = ref<Agent[]>([])
const loading = ref(false)
let watcherInitialized = false
let agentsWatcherInitialized = false
// initAgent dedupe: one in-flight GET /data_sources shared by all callers,
// and a short freshness window so the burst of mounts on a page load doesn't
// refetch. Kept short (seconds) so a just-created agent still shows up in
// selectors on the next navigation.
let inflightInit: Promise<void> | null = null
let lastInitAt = 0
const INIT_CACHE_TTL_MS = 10_000

// The org's whole agent inventory, for callers whose console is org-wide.
// `agents` only holds what the user is a member of, so an admin's monitoring
// selector would otherwise list a fraction of the agents their console is
// actually reporting on. Kept separate from `agents` so widening the console
// never widens the chat context or new-report defaults.
const consoleAgentPool = ref<Agent[]>([])
let inflightConsolePool: Promise<void> | null = null
let consolePoolLoadedAt = 0

export function useAgent() {
  // Set up watcher to persist selection changes (only once)
  if (!watcherInitialized && typeof window !== 'undefined') {
    watch(selectedAgents, (newSelection) => {
      saveToStorage(newSelection)
    }, { deep: true })
    watcherInitialized = true
  }

  // Watch for agents list changes and clean up stale selections
  // This handles the case when a user signs up, has no agents, then connects their first one
  if (!agentsWatcherInitialized && typeof window !== 'undefined') {
    let isFirstAgentsChange = true  // Track inside watcher to avoid async timing issues

    watch(agents, (newAgents, oldAgents) => {
      const oldCount = oldAgents?.length || 0
      const newCount = newAgents?.length || 0

      // First population (page load): skip reset to preserve persisted selection
      // The flag is managed inside the watcher to avoid Vue's async scheduling issues
      if (isFirstAgentsChange && oldCount === 0 && newCount > 0) {
        isFirstAgentsChange = false
        // Still clean up any stale selections (IDs that no longer exist)
        if (selectedAgents.value.length > 0) {
          const validIds = new Set(newAgents.map(a => a.id))
          const filtered = selectedAgents.value.filter(id => validIds.has(id))
          if (filtered.length !== selectedAgents.value.length) {
            selectedAgents.value = filtered
          }
        }
        return
      }

      // Subsequent 0->N changes (user connected first data source): reset to "All"
      if (newCount > oldCount && oldCount === 0) {
        selectedAgents.value = []
        return
      }

      // Clean up any stale selections (agent IDs that no longer exist)
      if (selectedAgents.value.length > 0 && newAgents?.length > 0) {
        const validIds = new Set(newAgents.map(a => a.id))
        const filtered = selectedAgents.value.filter(id => validIds.has(id))
        if (filtered.length !== selectedAgents.value.length) {
          selectedAgents.value = filtered
        }
      }
    }, { deep: true })
    agentsWatcherInitialized = true
  }

  // Computed: check if there are any agents
  const hasAgents = computed(() => agents.value.length > 0)

  // Computed: count of selected agents
  const selectedCount = computed(() => selectedAgents.value.length)

  // Computed: whether "All Agents" is effectively selected (no specific selection)
  const isAllAgents = computed(() => selectedAgents.value.length === 0)

  // Label for a selection within a given agent list. Shared so a scoped
  // selector (see `consoleAgents`) reads exactly like the unscoped one.
  function describeSelection(list: Agent[], selection: string[]): string {
    if (selection.length === 0) {
      // If only one agent exists, show its name instead of "All Agents"
      if (list.length === 1) {
        return list[0].name
      }
      return 'All'
    }
    if (selection.length === 1) {
      const agent = list.find(a => a.id === selection[0])
      return agent?.name || 'Selected Agent'
    }
    // Show first 2 agent names comma-separated, then +N for the rest
    const selectedObjs = list.filter(a => selection.includes(a.id))
    const first2 = selectedObjs.slice(0, 2).map(a => a.name)
    const remaining = selectedObjs.length - 2
    if (remaining > 0) {
      return `${first2.join(', ')} +${remaining}`
    }
    return first2.join(', ')
  }

  // Computed: get the current agent name (for display)
  const currentAgentName = computed(() => describeSelection(agents.value, selectedAgents.value))

  // Agents the monitoring console may report on, mirroring the backend gate in
  // app/core/console_access.py: an org admin runs the console org-wide, anyone
  // else sees only the agents they hold a `manage` grant on. Distinct from
  // `agents`, which is everything the user can *use*. Org-wide callers read the
  // full inventory (see `initConsoleAgents`), falling back to their own
  // memberships until it lands.
  const consoleAgents = computed(() => {
    if (useHasOrgWideConsole()) {
      return consoleAgentPool.value.length ? consoleAgentPool.value : agents.value
    }
    return agents.value.filter(a => useCan('manage', { type: 'data_source', id: a.id }))
  })

  // Load the org-wide inventory for the console selector. `show_all` is only
  // honoured for callers with org-wide data-source governance and ignored
  // otherwise, so this degrades to the normal list rather than failing.
  async function initConsoleAgents(opts: { force?: boolean } = {}) {
    if (!useHasOrgWideConsole()) return
    if (!opts.force) {
      if (inflightConsolePool) return inflightConsolePool
      if (consoleAgentPool.value.length && Date.now() - consolePoolLoadedAt < INIT_CACHE_TTL_MS) return
    }
    inflightConsolePool = (async () => {
      try {
        const { data } = await useMyFetch<Agent[]>('/data_sources', { method: 'GET', query: { show_all: true } })
        if (data.value) {
          consoleAgentPool.value = data.value
          consolePoolLoadedAt = Date.now()
        }
      } catch (error) {
        console.error('Failed to fetch the org-wide agent list:', error)
      }
    })()
    try {
      await inflightConsolePool
    } finally {
      inflightConsolePool = null
    }
  }
  const hasConsoleAgents = computed(() => consoleAgents.value.length > 0)

  // The current selection clamped to that set. Selection is global (shared with
  // the chat context), so it can name agents the user uses but doesn't manage —
  // those must never reach a console filter, which the API would reject.
  // Empty means "every agent in scope".
  const consoleSelectedAgents = computed(() => {
    const ids = new Set(consoleAgents.value.map(a => a.id))
    return selectedAgents.value.filter(id => ids.has(id))
  })
  const consoleAgentName = computed(() =>
    describeSelection(consoleAgents.value, consoleSelectedAgents.value)
  )
  // Watch key for the console pages. `consoleSelectedAgents` rebuilds a new
  // array on every dependency change, so a deep watcher on it re-fires (and
  // refetches) when the agent list merely loads. Comparing the joined ids means
  // a refetch happens only when the selection actually changes.
  const consoleSelectionKey = computed(() => consoleSelectedAgents.value.join(','))

  // Computed: get the selected agent objects
  const selectedAgentObjects = computed(() => {
    if (selectedAgents.value.length === 0) {
      return agents.value // All agents when none selected
    }
    return agents.value.filter(a => selectedAgents.value.includes(a.id))
  })

  // Toggle agent selection
  function toggleAgent(agentId: string | null) {
    if (agentId === null) {
      // "All Agents" selected - clear selection
      selectedAgents.value = []
      return
    }

    const index = selectedAgents.value.indexOf(agentId)
    if (index === -1) {
      // Add agent to selection
      selectedAgents.value = [...selectedAgents.value, agentId]
    } else {
      // Remove agent from selection
      selectedAgents.value = selectedAgents.value.filter(id => id !== agentId)
    }
  }

  // Check if an agent is selected
  function isAgentSelected(agentId: string): boolean {
    // If nothing is selected, all are considered selected
    if (selectedAgents.value.length === 0) {
      return false // Show as not individually selected when "All" is active
    }
    return selectedAgents.value.includes(agentId)
  }

  // Initialize agents by fetching from API.
  //
  // GET /data_sources is one of the heavier list endpoints and several
  // always-mounted components (layout, command palette, agent selector) call
  // initAgent() around the same time, so the page used to fire it 2-3× per
  // load. Dedupe: callers share one in-flight request, and a result fresher
  // than the TTL is reused instead of refetched. Pass { force: true } to
  // bypass (e.g. right after creating/deleting an agent).
  async function initAgent(opts: { force?: boolean } = {}) {
    if (!opts.force) {
      if (inflightInit) return inflightInit
      if (agents.value.length > 0 && Date.now() - lastInitAt < INIT_CACHE_TTL_MS) return
    }
    inflightInit = (async () => {
      loading.value = true
      try {
        const { data } = await useMyFetch<Agent[]>('/data_sources', { method: 'GET' })
        if (data.value) {
          agents.value = data.value
          lastInitAt = Date.now()
        }
      } catch (error) {
        console.error('Failed to fetch agents:', error)
      } finally {
        loading.value = false
      }
    })()
    try {
      await inflightInit
    } finally {
      inflightInit = null
    }
  }

  // Set agents directly (for external initialization)
  function setAgents(newAgents: Agent[]) {
    agents.value = newAgents
  }

  // Clear selection
  function clearSelection() {
    selectedAgents.value = []
  }

  // Select specific agents
  function selectAgents(agentIds: string[]) {
    selectedAgents.value = agentIds
  }

  return {
    // State
    selectedAgents: readonly(selectedAgents),
    agents: readonly(agents),
    loading: readonly(loading),

    // Computed
    hasAgents,
    selectedCount,
    isAllAgents,
    currentAgentName,
    selectedAgentObjects,
    consoleAgents,
    hasConsoleAgents,
    consoleSelectedAgents,
    consoleAgentName,
    consoleSelectionKey,

    // Methods
    toggleAgent,
    isAgentSelected,
    initAgent,
    initConsoleAgents,
    setAgents,
    clearSelection,
    selectAgents,
  }
}
