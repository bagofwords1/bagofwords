/**
 * Agent selection composable.
 * Manages which agents (data sources) are currently selected/filtered.
 *
 * The selection IS the user's agent scope, and an empty selection is Auto —
 * "whatever I can access, resolved per run" — not "every agent, pinned". See
 * utils/agentSelection.ts; the distinction is the whole reason
 * `selectedAgentObjects` and `effectiveAgentObjects` are separate below.
 *
 * It persists per user per organization in `memberships.default_data_source_ids`
 * (GET/PUT /users/me/default_agents), with localStorage as a same-origin cache
 * so the prompt box renders the right scope on first paint instead of flashing
 * Auto until the request lands. The server value always wins on reconcile.
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

// Cache key for the agent selection. Org-scoped: the scope lives on the
// membership, so one workspace's pinned agents must not surface as another's —
// the ids wouldn't even resolve there.
const STORAGE_KEY = 'bow_selected_agents'
const LEGACY_STORAGE_KEY = 'bow_selected_domains'
const storageKeyFor = (orgId: string | null) => (orgId ? `${STORAGE_KEY}:${orgId}` : STORAGE_KEY)

function readJsonArray(key: string): string[] | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.map((x: any) => String(x)) : null
  } catch {
    return null
  }
}

// Load the cached selection for an org. Falls back to the pre-org-scoped keys
// once, so an existing user's pinned agents survive the move to per-org keys
// (and the older `bow_selected_domains` rename before it).
function loadFromStorage(orgId: string | null): string[] {
  if (typeof window === 'undefined') return []
  const scoped = readJsonArray(storageKeyFor(orgId))
  if (scoped) return scoped
  return readJsonArray(STORAGE_KEY) || readJsonArray(LEGACY_STORAGE_KEY) || []
}

// Save selection to localStorage
function saveToStorage(agentIds: string[], orgId: string | null) {
  if (typeof window === 'undefined') return
  try {
    localStorage.setItem(storageKeyFor(orgId), JSON.stringify(agentIds))
  } catch (e) {
    console.warn('Failed to save agent selection:', e)
  }
}

// Global state (shared across components). Starts on Auto and is filled by
// initAgentPreference() — the org isn't known at module-evaluation time, and
// the cache is keyed by it.
const selectedAgents = ref<string[]>([])
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

// Persistence state for the per-user agent scope. `preferenceOrgId` is the org
// the current selection belongs to, so a workspace switch reloads rather than
// saving one org's ids onto another's membership.
let preferenceOrgId: string | null = null
let preferenceLoaded = false
let inflightPreference: Promise<void> | null = null
let saveTimer: ReturnType<typeof setTimeout> | null = null
// What the membership is known to hold. Applying the loaded scope trips the
// watcher — Vue flushes it a tick later, by which point the load has finished —
// so without this every page load would PUT the value it just read back,
// overwriting anything another tab or device changed in between.
let serverIds: string[] | null = null
// Selections come in bursts (a click on one row can clear a pin and set
// another), and each one is a PUT. Coalesce them.
const PREFERENCE_SAVE_DEBOUNCE_MS = 400

const sameIds = (a: string[], b: string[]) =>
  a.length === b.length && a.every((id, i) => id === b[i])

export function useAgent() {
  // Set up watcher to persist selection changes (only once). The cache write is
  // synchronous so a reload paints the right scope immediately; the server
  // write is debounced and only runs once the preference has been loaded — a
  // watcher firing during the initial load would otherwise PUT the cached value
  // straight back and undo a change made from another device.
  if (!watcherInitialized && typeof window !== 'undefined') {
    watch(selectedAgents, (newSelection) => {
      const ids = [...newSelection]
      saveToStorage(ids, preferenceOrgId)
      if (!preferenceLoaded) return
      // Already what the membership holds — either this IS the loaded value
      // arriving, or the user landed back on it mid-debounce. Drop any pending
      // write too: the intermediate value it carries is no longer the scope.
      if (serverIds && sameIds(ids, serverIds)) {
        if (saveTimer) { clearTimeout(saveTimer); saveTimer = null }
        return
      }
      if (saveTimer) clearTimeout(saveTimer)
      saveTimer = setTimeout(() => { saveTimer = null; void persistPreference(ids) }, PREFERENCE_SAVE_DEBOUNCE_MS)
    }, { deep: true })
    watcherInitialized = true
  }

  // Write the scope to the caller's membership. Failures are non-fatal: the
  // selection still applies to this session and stays in the local cache, so a
  // blip in this request never blocks the user from picking an agent.
  async function persistPreference(ids: string[]) {
    try {
      const { data } = await useMyFetch<{ data_source_ids: string[] }>('/users/me/default_agents', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data_source_ids: ids }),
      })
      // The server prunes ids the user may no longer pin, so reconcile — but
      // only if the user hasn't moved on to a different selection since.
      const stored = (data.value as any)?.data_source_ids
      if (!Array.isArray(stored)) return
      serverIds = [...stored]
      if (sameIds([...selectedAgents.value], ids) && !sameIds(stored, ids)) {
        selectedAgents.value = stored
      }
    } catch (error) {
      console.warn('Failed to persist agent selection:', error)
    }
  }

  // Load the stored scope for the active org: cache first (so first paint is
  // right), then the membership value, which wins. Safe to call from several
  // components — concurrent callers share one request, and it only reloads when
  // the org changes or `force` is passed.
  async function initAgentPreference(opts: { force?: boolean } = {}) {
    if (typeof window === 'undefined') return
    const { organization, ensureOrganization } = useOrganization()
    await ensureOrganization()
    const orgId = organization.value?.id || null

    if (!opts.force && preferenceLoaded && preferenceOrgId === orgId) return
    if (!opts.force && inflightPreference && preferenceOrgId === orgId) return inflightPreference

    if (preferenceOrgId !== orgId) {
      // New workspace: drop the old org's scope before anything can save it
      // back, and stop treating it as loaded.
      preferenceLoaded = false
      preferenceOrgId = orgId
      serverIds = null
      if (saveTimer) { clearTimeout(saveTimer); saveTimer = null }
      selectedAgents.value = loadFromStorage(orgId)
    }

    inflightPreference = (async () => {
      try {
        const { data } = await useMyFetch<{ data_source_ids: string[] }>('/users/me/default_agents')
        const ids = (data.value as any)?.data_source_ids
        // No usable answer (useMyFetch reports HTTP failures through `error`,
        // not by throwing) — leave `preferenceLoaded` false so we keep the
        // cached scope and never write a guess back over the stored one.
        if (!Array.isArray(ids)) return
        const loaded = ids.map((x: any) => String(x))
        serverIds = [...loaded]
        if (!sameIds(loaded, [...selectedAgents.value])) {
          selectedAgents.value = loaded
        }
        preferenceLoaded = true
      } catch (error) {
        // Offline or a 5xx: keep the cached scope for this session rather than
        // silently resetting the user to Auto. Leaving `preferenceLoaded` false
        // also keeps us from writing that guess back to the membership.
        console.warn('Failed to load agent selection:', error)
      }
    })()
    try {
      await inflightPreference
    } finally {
      inflightPreference = null
    }
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

  // The agents the user actually pinned. Empty under Auto — and it must STAY
  // empty: Auto is the absence of a pin, not a pin covering everything (see
  // utils/agentSelection.ts). Fanning it out here is what used to make the
  // prompt box open with every agent checked, so picking one meant unchecking
  // the rest, and what made every new report freeze today's roster instead of
  // resolving its scope per run.
  //
  // A pinned id the loaded list doesn't cover yet keeps its place as a bare
  // {id} rather than dropping out — same reasoning as `alignSelection` in
  // DataSourceSelector. Dropping it would WIDEN the scope (an empty selection
  // is Auto), and since the saved scope arrives before `agents` does, dropping
  // would briefly render Auto and let that echo back as a real change,
  // overwriting the user's pin with Auto. Stale ids are cleaned by the watcher
  // below, once the list is actually loaded.
  const agentsById = computed(() => new Map(agents.value.map(a => [a.id, a])))
  const selectedAgentObjects = computed(() =>
    selectedAgents.value.map(id => agentsById.value.get(id) || ({ id, connections: [] } as unknown as Agent))
  )

  // The agents the current selection RESOLVES to right now — the pinned ones,
  // or the whole roster under Auto. For callers that need something concrete to
  // render or fetch against (starter prompts, the instructions panel). Never
  // use this to build a report's `data_sources`: it turns Auto into a pin.
  const effectiveAgentObjects = computed(() =>
    selectedAgents.value.length === 0 ? agents.value : selectedAgentObjects.value
  )

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
    effectiveAgentObjects,
    consoleAgents,
    hasConsoleAgents,
    consoleSelectedAgents,
    consoleAgentName,
    consoleSelectionKey,

    // Methods
    toggleAgent,
    isAgentSelected,
    initAgent,
    initAgentPreference,
    initConsoleAgents,
    setAgents,
    clearSelection,
    selectAgents,
  }
}
