import type { WorldNode, WorldEdge, WorldType } from '~/composables/useWorldGraph'

// Categorical visualization colors, independent of semantic UI text tokens.
export const worldColors: Record<WorldType, string> = {
  task: '#6793ba', folder: '#cb974b', eval: '#cd7288', agent: '#238b77', connection: '#518ed0', instruction: '#aa85db', table: '#66b8a8',
  file: '#dcaa66', tool: '#6b85de', list: '#b19c58', project: '#c783a0', report: '#8fa1c1',
  artifact: '#d18a75', query: '#83b97c', step: '#66abc1', completion: '#acb3bf',
  execution: '#d9a36f', memory: '#aa77d0',
}
export const worldIcons: Record<WorldType, string> = {
  task: 'i-heroicons-clock', folder: 'i-heroicons-folder', eval: 'i-heroicons-beaker', agent: 'i-heroicons-cube-transparent', connection: 'i-heroicons-circle-stack',
  instruction: 'i-heroicons-document-text', table: 'i-heroicons-table-cells',
  file: 'i-heroicons-document', tool: 'i-heroicons-bolt', list: 'i-heroicons-list-bullet',
  project: 'i-heroicons-folder', report: 'i-heroicons-chat-bubble-left-right',
  artifact: 'i-heroicons-squares-2x2', query: 'i-heroicons-command-line',
  step: 'i-heroicons-arrow-path', completion: 'i-heroicons-chat-bubble-bottom-center-text',
  execution: 'i-heroicons-play', memory: 'i-heroicons-sparkles',
}
export interface WorldPoint { node: WorldNode; x: number; y: number; radius: number; phase: number }
function hash(s: string) { let n = 2166136261; for (const c of s) n = Math.imul(n ^ c.charCodeAt(0), 16777619); return n >>> 0 }

/** Stable positions: filtering never reshuffles the world. Only real resources
 * are drawn. Placement is a navigation aid, never an inferred relationship. */
export function layoutWorld(nodes: WorldNode[], edges: WorldEdge[]): WorldPoint[] {
  const contained = new Set(edges.filter(e => e.relation === 'contains' && e.source.startsWith('folder:')).map(e => e.target))
  const agents = nodes.filter(n => (n.type === 'folder' || n.type === 'agent') && !contained.has(n.id)).sort((a, b) => a.id.localeCompare(b.id))
  const anchors = new Map(agents.map((a, i) => {
    const angle = i * Math.PI * 2 / Math.max(agents.length, 1)
    const r = agents.length === 1 ? 0 : 205 + Math.min(agents.length, 12) * 5
    return [a.id, { x: Math.cos(angle) * r, y: Math.sin(angle) * r }]
  }))
  const adjacent = new Map<string, string[]>()
  for (const edge of edges) {
    for (const [from, to] of [[edge.source, edge.target], [edge.target, edge.source]]) {
      if (!adjacent.has(from)) adjacent.set(from, [])
      adjacent.get(from)!.push(to)
    }
  }
  // Assign each resource to its closest root. Traverse arbitrary folder depth.
  const parent = new Map(agents.map(root => [root.id, root.id]))
  const queue = agents.map(root => root.id)
  for (let i = 0; i < queue.length; i++) {
    const from = queue[i]
    for (const to of adjacent.get(from) || []) {
      if (parent.has(to)) continue
      parent.set(to, parent.get(from)!)
      queue.push(to)
    }
  }
  const groups = new Map<string, WorldNode[]>()
  for (const n of nodes) {
    if (anchors.has(n.id)) continue
    const group = n.type === 'memory' ? 'memory' : parent.get(n.id) || 'unassigned'
    if (!groups.has(group)) groups.set(group, [])
    groups.get(group)!.push(n)
  }
  const points: WorldPoint[] = agents.map(n => ({ node: n, ...anchors.get(n.id)!, radius: 12, phase: 0 }))
  for (const [group, children] of groups) {
    children.sort((a, b) => a.id.localeCompare(b.id))
    const center = anchors.get(group) || (group === 'memory' ? { x: 0, y: 330 } : { x: 0, y: 0 })
    children.forEach((node, i) => {
      const phase = hash(node.id) / 4294967295 * Math.PI * 2
      const angle = i * 2.399963229728653 + (hash(group) % 100) / 100
      const distance = 34 + Math.sqrt(i + 1) * (children.length > 200 ? 8 : 17)
      points.push({ node, x: center.x + Math.cos(angle) * distance,
        y: center.y + Math.sin(angle) * distance * .86, phase,
        radius: node.type === 'memory' ? 5.5 : node.type === 'connection' ? 7 : 3.5 + (hash(node.id) % 3) * .7 })
    })
  }
  return points
}
