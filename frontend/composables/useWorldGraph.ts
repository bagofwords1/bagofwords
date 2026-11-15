export const worldTypes = ['task', 'folder', 'eval', 'agent', 'connection', 'instruction', 'table', 'file', 'tool', 'list', 'project', 'report', 'artifact', 'query', 'step', 'completion', 'execution', 'memory'] as const
export type WorldType = typeof worldTypes[number]
export interface WorldNode {
  id: string; resource_id: string; type: WorldType; label: string; description: string
  url?: string; expandable: boolean; meta: Record<string, unknown>
}
export interface WorldEdge { id: string; source: string; target: string; relation: string; report_count?: number }
export interface WorldRequest { section: string; offset?: number; folder_id?: string; agent_id?: string; report_id?: string }
interface WorldPage { nodes: WorldNode[]; edges: WorldEdge[]; next: WorldRequest[] }

export function useWorldGraph() {
  const nodes = shallowRef<WorldNode[]>([])
  const edges = shallowRef<WorldEdge[]>([])
  const pending = ref(0)
  const initializing = ref(false)
  const loadingAll = ref(false)
  const failures = ref<WorldRequest[]>([])
  const next = ref<WorldRequest[]>([])
  const usageLoaded = ref(new Set<string>())
  const expanded = ref(new Set<string>())
  const { organization } = useOrganization()
  const route = useRoute()
  let generation = 0
  let disposed = false
  const requestKey = (r: WorldRequest) => JSON.stringify(r)
  const inflight = new Set<string>()

  async function load(request: WorldRequest, epoch = generation) {
    const k = requestKey(request)
    if (disposed || epoch !== generation || inflight.has(k)) return
    inflight.add(k); pending.value++
    try {
      const { data, error } = await useMyFetch<WorldPage>('/api/world', { query: request })
      if (disposed || epoch !== generation) return
      if (request.section === 'overview' && [403, 404].includes((error.value as any)?.statusCode)) {
        await navigateTo('/agents', { replace: true })
        return
      }
      if (error.value || !data.value) { failures.value = [...failures.value.filter(r => requestKey(r) !== k), request]; return }
      const page = data.value
      if (request.section === 'usage') usageLoaded.value = new Set([...usageLoaded.value, request.agent_id || 'all'])
      if (request.section === 'memory') clearMemory()
      const merged = new Map(nodes.value.map(n => [n.id, n]))
      for (const node of page.nodes) {
        const previous = merged.get(node.id)
        merged.set(node.id, { ...node, url: node.url || previous?.url,
          description: node.description || previous?.description || '', meta: { ...previous?.meta, ...node.meta } })
      }
      nodes.value = [...merged.values()]
      edges.value = [...new Map([...edges.value, ...page.edges].map(e => [e.id, e])).values()]
      next.value = [...new Map([...next.value.filter(r => requestKey(r) !== k), ...page.next].map(r => [requestKey(r), r])).values()]
      failures.value = failures.value.filter(r => requestKey(r) !== k)
      if (request.folder_id) expanded.value = new Set([...expanded.value, `folder:${request.folder_id}`])
      if (request.section === 'agent' && request.agent_id) expanded.value = new Set([...expanded.value, `agent:${request.agent_id}`])
      if (request.report_id) expanded.value = new Set([...expanded.value, `report:${request.report_id}`])
    } catch {
      if (!disposed && epoch === generation) failures.value = [...failures.value.filter(r => requestKey(r) !== k), request]
    } finally {
      if (epoch === generation) { inflight.delete(k); pending.value-- }
    }
  }
  async function batch(requests: WorldRequest[], epoch = generation) {
    const queue = [...requests]
    await Promise.all(Array.from({ length: Math.min(3, queue.length) }, async () => {
      while (queue.length && !disposed && epoch === generation) await load(queue.shift()!, epoch)
    }))
  }
  async function refresh() {
    loadingAll.value = false
    const epoch = ++generation
    usageLoaded.value = new Set()
    initializing.value = true
    nodes.value = []; edges.value = []; next.value = []; failures.value = []
    pending.value = 0; expanded.value = new Set(); inflight.clear()
    try {
      await load({ section: 'overview' }, epoch)
      if (epoch !== generation || disposed || failures.value.length) return
      next.value = nodes.value.filter(n => n.type === 'agent' || n.type === 'folder').map(n => ({ section: n.type, [`${n.type}_id`]: n.resource_id }))
      await batch([
        ...['tasks', 'evals', 'instructions', 'files', 'projects', 'reports', 'artifacts'].map(section => ({ section })),
      ], epoch)
    } finally { if (epoch === generation) initializing.value = false }
  }
  function expand(node: WorldNode) {
    return load({ section: node.type, [`${node.type}_id`]: node.resource_id })
  }
  function clearMemory() {
    const ids = new Set(nodes.value.filter(n => n.type === 'memory').map(n => n.id))
    nodes.value = nodes.value.filter(n => n.type !== 'memory')
    edges.value = edges.value.filter(e => !ids.has(e.source) && !ids.has(e.target))
  }
  async function loadAll() {
    const epoch = generation
    loadingAll.value = true
    try {
      while (next.value.length && loadingAll.value && epoch === generation && !disposed) {
        await batch(next.value.slice(0, 3), epoch)
        if (failures.value.length) break
      }
    } finally { if (epoch === generation) loadingAll.value = false }
  }
  onMounted(async () => { await refresh(); if (route.query.load === 'all') await loadAll() })
  watch(() => organization.value?.id, (id, old) => { if (id && old && id !== old) refresh() })
  onBeforeUnmount(() => { disposed = true; generation++ })
  return { nodes, edges, usageLoaded, pending, initializing, loadingAll, failures, next, expanded, refresh, load, expand, clearMemory, loadAll,
    stopLoading: () => { loadingAll.value = false },
    loadMore: () => batch(next.value.slice(0, 5)), retry: () => batch(failures.value) }
}
